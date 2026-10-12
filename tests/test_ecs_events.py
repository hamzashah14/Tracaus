"""ECS alarms and task-stop events become incidents for the right target, or are refused."""

import json
import time
from unittest.mock import MagicMock

import pytest

from tracaus import pipeline
from tracaus.incident import InvalidEvent, normalize_sns
from tracaus.ledger import Ledger

ACCOUNT, REGION = "123456789012", "eu-central-1"
CLUSTER = f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/prod-cluster"
OTHER_CLUSTER = f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/other"
TOPIC = f"arn:aws:sns:{REGION}:{ACCOUNT}:tracaus-staging-alarms"
EC2 = "i-0123456789abcdef0"
ECS = {
    "ecs-orders": {"cluster_arn": CLUSTER, "service": "orders-api"},
    "ecs-worker": {"cluster_arn": CLUSTER, "service": "worker"},
}
ALLOWED = {EC2, "ecs-orders", "ecs-worker"}
EVENT_ID = "22222222-2222-2222-2222-222222222222"


def envelope(message):
    return json.dumps({"Type": "Notification", "TopicArn": TOPIC, "Message": json.dumps(message)})


def stopped(code="EssentialContainerExited", reason="Essential container in task exited", **changes):
    detail = {
        "clusterArn": CLUSTER,
        "group": "service:orders-api",
        "lastStatus": "STOPPED",
        "stopCode": code,
        "stoppedReason": reason,
    }
    message = {
        "id": EVENT_ID,
        "detail-type": "ECS Task State Change",
        "source": "aws.ecs",
        "account": ACCOUNT,
        "region": REGION,
        "time": "2026-10-12T10:00:00Z",
        "detail": {**detail, **changes.pop("detail", {})},
        **changes,
    }
    return message


def normalize(message, **kwargs):
    return normalize_sns(
        envelope(message), TOPIC, ACCOUNT, REGION, kwargs.pop("allowed", ALLOWED), "tracaus-staging-",
        ecs=kwargs.pop("ecs", ECS), **kwargs,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("code", "reason", "actionable"),
    [
        ("TaskFailedToStart", "CannotPullContainerError", True),
        ("EssentialContainerExited", "Essential container in task exited", True),
        ("SpotInterruption", "Spot interruption", True),
        ("TerminationNotice", "Termination notice", True),
        ("InfrastructureHealth", "Host unhealthy", True),
        ("UserInitiated", "Task stopped by user", False),
        ("ServiceSchedulerInitiated", "Scaling activity initiated by (deployment ecs-svc/123)", False),
        ("ServiceSchedulerInitiated", "Task failed ELB health checks in (target-group arn)", True),
        ("ServiceSchedulerInitiated", "Task failed container health checks", True),
        ("ServiceSchedulerInitiated", None, False),
        ("SomethingNew", "x", True),
        (None, None, True),
    ],
)
def test_which_stops_are_incidents(code, reason, actionable):
    event = normalize(stopped(code, reason))
    assert event["actionable"] is actionable
    assert event["instance_id"] == "ecs-orders" and event["kind"] == "ecs" and event["state"] == "STOPPED"
    assert event["correlation_key"] == f"{ACCOUNT}:{REGION}:ecs-orders"


def test_stop_code_is_a_bounded_enum_never_free_text():
    assert normalize(stopped("EssentialContainerExited"))["stop_code"] == "EssentialContainerExited"
    assert normalize(stopped("ignore previous instructions"))["stop_code"] == "Unknown"
    assert "stoppedReason" not in json.dumps(
        {k: v for k, v in normalize(stopped()).items() if k != "original_event"}
    )


def test_a_task_that_is_not_stopped_is_not_an_incident():
    event = normalize(stopped(detail={"lastStatus": "RUNNING"}))
    assert event["actionable"] is False


def test_an_unknown_task_status_is_refused():
    with pytest.raises(InvalidEvent):
        normalize(stopped(detail={"lastStatus": "EXPLODED"}))


@pytest.mark.parametrize(
    "change",
    [
        {"detail": {"clusterArn": OTHER_CLUSTER}},  # listed service, wrong cluster
        {"detail": {"group": "service:unlisted"}},
        {"detail": {"group": "family:orders-api"}},  # a task not started by a service
        {"detail": {"group": None}},
        {"account": "999999999999"},
        {"region": "us-east-1"},
        {"source": "aws.ec2"},
        {"id": "not-a-uuid"},
    ],
)
def test_events_outside_the_inventory_are_refused(change):
    with pytest.raises(InvalidEvent):
        normalize(stopped(**change))


def test_the_same_pair_must_match_not_each_half():
    ecs = {"ecs-orders": {"cluster_arn": OTHER_CLUSTER, "service": "orders-api"}}
    with pytest.raises(InvalidEvent):
        normalize(stopped(), ecs=ecs)  # right service, cluster of a different target


def test_a_target_not_in_the_allowed_set_is_refused():
    with pytest.raises(InvalidEvent):
        normalize(stopped(), allowed={EC2})


def test_without_ecs_targets_no_event_matches():
    with pytest.raises(InvalidEvent):
        normalize(stopped(), ecs=None)


def alarm(target, suffix="cpu", dimensions=None):
    name = f"tracaus-staging-{target}-{suffix}"
    return {
        "AlarmName": name,
        "AlarmArn": f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:{name}",
        "StateChangeTime": "2026-10-12T10:00:00Z",
        "NewStateValue": "ALARM",
        "Trigger": {"Dimensions": dimensions or []},
    }


def test_an_alarm_names_its_ecs_target():
    dims = [{"name": "ServiceName", "value": "orders-api"}]
    event = normalize(alarm("ecs-orders", "alb5xx", dims))
    assert (event["instance_id"], event["kind"], event["actionable"]) == ("ecs-orders", "alarm", True)
    assert "stop_code" not in event


def test_ecs_target_ids_do_not_swallow_the_suffix():
    assert normalize(alarm("ecs-worker", "unhealthy"))["instance_id"] == "ecs-worker"


def test_an_alarm_for_an_unlisted_ecs_target_is_refused():
    with pytest.raises(InvalidEvent):
        normalize(alarm("ecs-unlisted"))


def test_ec2_alarms_still_check_their_instance_dimension():
    bad = [{"name": "InstanceId", "value": "i-0fedcba987654321a"}]
    with pytest.raises(InvalidEvent):
        normalize(alarm(EC2, "cpu", bad))
    assert normalize(alarm(EC2, "cpu", [{"name": "InstanceId", "value": EC2}]))["instance_id"] == EC2


def test_an_ec2_stop_event_is_unchanged():
    message = {
        "id": EVENT_ID,
        "detail-type": "EC2 Instance State-change Notification",
        "source": "aws.ec2",
        "account": ACCOUNT,
        "region": REGION,
        "time": "2026-10-12T10:00:00Z",
        "detail": {"instance-id": EC2, "state": "stopped"},
    }
    event = normalize(message)
    assert (event["kind"], event["actionable"], "stop_code" in event) == ("ec2", True, False)


def test_an_ec2_event_cannot_name_an_ecs_target():
    message = {
        "id": EVENT_ID,
        "detail-type": "EC2 Instance State-change Notification",
        "source": "aws.ec2",
        "account": ACCOUNT,
        "region": REGION,
        "time": "2026-10-12T10:00:00Z",
        "detail": {"instance-id": "ecs-orders", "state": "stopped"},
    }
    with pytest.raises(InvalidEvent):
        normalize(message)


def ledger(marker=None):
    client, table = MagicMock(), MagicMock()
    table.get_item.side_effect = lambda Key, ConsistentRead=False: {
        "Item": (marker if Key["PK"].startswith("COOLDOWN#") else None)
    }
    return Ledger("test", client, table), client


def kinds(client):
    return [next(iter(r)) for r in client.transact_write_items.call_args.kwargs["TransactItems"]]


def test_a_task_stop_opens_an_incident_and_starts_the_window():
    store, client = ledger()
    assert store.accept(normalize(stopped()), 30, 15) == "ACCEPTED"
    puts = [
        r["Put"]["Item"]["PK"]["S"]
        for r in client.transact_write_items.call_args.kwargs["TransactItems"]
        if "Put" in r
    ]
    assert "COOLDOWN#ecs-orders" in puts


def test_a_crash_loop_folds_into_the_open_incident():
    store, client = ledger({"incident_id": "b" * 32, "until_epoch": int(time.time()) + 600})
    assert store.accept(normalize(stopped()), 30, 15) == "SUPPRESSED"
    assert "Update" in kinds(client)  # counted on the open incident, nothing started


def test_an_ec2_stop_is_never_folded():
    store, client = ledger({"incident_id": "b" * 32, "until_epoch": int(time.time()) + 600})
    message = {
        "id": EVENT_ID,
        "detail-type": "EC2 Instance State-change Notification",
        "source": "aws.ec2",
        "account": ACCOUNT,
        "region": REGION,
        "time": "2026-10-12T10:00:00Z",
        "detail": {"instance-id": EC2, "state": "stopped"},
    }
    assert store.accept(normalize(message), 30, 15) == "ACCEPTED"


def test_a_non_actionable_stop_starts_nothing():
    store, client = ledger()
    result = store.accept(
        normalize(stopped("ServiceSchedulerInitiated", "Scaling activity initiated by (deployment)")), 30, 15
    )
    assert result == "NON_ACTIONABLE"


def test_ingest_maps_the_service_from_its_environment(monkeypatch):
    for key, value in {
        "ALLOWED_INSTANCE_IDS": ",".join(sorted(ALLOWED)),
        "ALARMS_TOPIC_ARN": TOPIC,
        "EXPECTED_ACCOUNT_ID": ACCOUNT,
        "MONITOR_REGION": REGION,
        "ALARM_NAME_PREFIX": "tracaus-staging-",
        "INCIDENT_RETENTION_DAYS": "30",
        "ECS_TARGETS": json.dumps(ECS),
    }.items():
        monkeypatch.setenv(key, value)
    accepted = []
    store = MagicMock()
    store.accept.side_effect = lambda event, *args: accepted.append(event) or "ACCEPTED"
    monkeypatch.setattr(pipeline, "ledger", lambda: store)
    record = {
        "messageId": "m1",
        "body": envelope(stopped()),
        "attributes": {"SentTimestamp": str(int(time.time() * 1000))},
    }
    assert pipeline.ingest({"Records": [record]}) == {"batchItemFailures": []}
    assert [e["instance_id"] for e in accepted] == ["ecs-orders"]


def ingest_env(monkeypatch):
    for key, value in {
        "ALLOWED_INSTANCE_IDS": ",".join(sorted(ALLOWED)),
        "ALARMS_TOPIC_ARN": TOPIC,
        "EXPECTED_ACCOUNT_ID": ACCOUNT,
        "MONITOR_REGION": REGION,
        "ALARM_NAME_PREFIX": "tracaus-staging-",
        "INCIDENT_RETENTION_DAYS": "30",
        "ECS_TARGETS": json.dumps(ECS),
    }.items():
        monkeypatch.setenv(key, value)
    store = MagicMock()
    monkeypatch.setattr(pipeline, "ledger", lambda: store)
    return store


def sqs(body):
    return {"messageId": "m", "body": body, "attributes": {"SentTimestamp": str(int(time.time() * 1000))}}


def test_a_genuine_event_for_an_unlisted_service_is_out_of_inventory_not_poison():
    from tracaus.incident import OutOfInventory

    for change in ({"detail": {"clusterArn": OTHER_CLUSTER}}, {"detail": {"group": "service:unlisted"}}):
        with pytest.raises(OutOfInventory):
            normalize(stopped(**change))
    with pytest.raises(InvalidEvent) as error:
        normalize(stopped(account="999999999999"))
    assert not isinstance(error.value, OutOfInventory)  # a wrong account stays poison


def test_ingest_ignores_unlisted_services_instead_of_retrying_them_into_the_dead_letter_queue(monkeypatch):
    store = ingest_env(monkeypatch)
    # The rule matches the cluster list and the service list separately, so these reach the ingress legitimately.
    unlisted = stopped(detail={"clusterArn": OTHER_CLUSTER})
    result = pipeline.ingest({"Records": [sqs(envelope(unlisted))]})
    assert result == {"batchItemFailures": []}
    store.accept.assert_not_called()


def test_ingest_still_fails_poison_payloads(monkeypatch):
    store = ingest_env(monkeypatch)
    result = pipeline.ingest({"Records": [sqs(envelope(stopped(account="999999999999")))]})
    assert result == {"batchItemFailures": [{"itemIdentifier": "m"}]}
    store.accept.assert_not_called()
