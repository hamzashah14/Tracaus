"""Per-instance cooldown: later alarms for an instance with an open incident are stored and counted, not investigated."""

import json
import time
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from infra.deploy import durable
from infra.spec import ROOT, load
from scripts.dev import validate_durable
from tracaus import pipeline
from tracaus.incident import normalize_sns
from tracaus.ledger import Ledger

ACCOUNT, REGION = "123456789012", "eu-central-1"
IID = "i-0123456789abcdef0"
PARENT = "b" * 32
TOPIC = f"arn:aws:sns:{REGION}:{ACCOUNT}:tracaus-staging-alarms"


def event(kind="alarm", suffix="cpu", at="2026-10-09T10:00:00Z", **extra):
    alarm = {
        "AlarmName": f"tracaus-staging-{IID}-{suffix}",
        "AlarmArn": f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:tracaus-staging-{IID}-{suffix}",
        "StateChangeTime": at,
        "NewStateValue": "ALARM",
        "Trigger": {"Dimensions": [{"name": "InstanceId", "value": IID}]},
    }
    ec2 = {
        "id": "11111111-1111-1111-1111-111111111111",
        "detail-type": "EC2 Instance State-change Notification",
        "source": "aws.ec2",
        "account": ACCOUNT,
        "region": REGION,
        "time": at,
        "detail": {"instance-id": IID, "state": "stopped"},
    }
    raw = json.dumps(
        {"Type": "Notification", "TopicArn": TOPIC, "Message": json.dumps(alarm if kind == "alarm" else ec2)}
    )
    return {**normalize_sns(raw, TOPIC, ACCOUNT, REGION, {IID}, "tracaus-staging-"), **extra}


def store(marker=None, **tables):
    client, table = MagicMock(), MagicMock()
    table.get_item.side_effect = lambda Key, ConsistentRead=False: {
        "Item": (marker if Key["PK"].startswith("COOLDOWN#") else None)
    }
    return Ledger("test", client, table), client


def written(client):
    records = client.transact_write_items.call_args.kwargs["TransactItems"]
    return [next(iter(r)) for r in records], records


def window(seconds):
    return {"incident_id": PARENT, "until_epoch": int(time.time()) + seconds}


def test_first_alarm_opens_an_incident_and_starts_the_window():
    ledger, client = store()
    assert ledger.accept(event(), 30, 15) == "ACCEPTED"
    kinds, records = written(client)
    assert kinds.count("Put") >= 5  # event, incident, two intents, notification, marker
    marker = next(r["Put"] for r in records if r["Put"]["Item"]["PK"]["S"] == f"COOLDOWN#{IID}")
    assert marker["ConditionExpression"] == "attribute_not_exists(PK) OR until_epoch<=:now"
    assert int(marker["Item"]["until_epoch"]["N"]) - int(time.time()) in range(14 * 60, 15 * 60 + 2)


def test_alarm_inside_the_window_is_stored_and_counted_but_starts_nothing():
    ledger, client = store(window(600))
    assert ledger.accept(event(), 30, 15) == "SUPPRESSED"
    kinds, records = written(client)
    assert kinds == ["Put", "ConditionCheck", "Update"]
    row = records[0]["Put"]["Item"]
    assert row["PK"]["S"].startswith("EVENT#") and row["suppressed_by"]["S"] == PARENT
    assert records[1]["ConditionCheck"]["ConditionExpression"] == "incident_id=:parent AND until_epoch>:now"
    assert records[2]["Update"]["Key"]["PK"]["S"] == f"INCIDENT#{PARENT}"
    assert records[2]["Update"]["UpdateExpression"] == "ADD suppressed_alarms :one"


def test_an_expired_window_opens_a_new_incident():
    ledger, client = store(window(-5))
    assert ledger.accept(event(), 30, 15) == "ACCEPTED"
    assert any(r["Put"]["Item"]["PK"]["S"] == f"COOLDOWN#{IID}" for r in written(client)[1] if "Put" in r)


def test_an_ec2_stop_is_never_folded_and_leaves_the_window_alone():
    ledger, client = store(window(600))
    assert ledger.accept(event("ec2"), 30, 15) == "ACCEPTED"
    assert not any(
        r.get("Put", {}).get("Item", {}).get("PK", {}).get("S", "").startswith("COOLDOWN#")
        for r in written(client)[1]
    )


def test_zero_minutes_is_the_old_behaviour():
    ledger, client = store(window(600))
    assert ledger.accept(event(), 30, 0) == "ACCEPTED"
    assert not any("COOLDOWN#" in json.dumps(r) for r in written(client)[1])
    assert ledger.accept(event(), 30) == "ACCEPTED"  # a direct Ledger call without the setting


def test_recovery_of_a_folded_alarm_points_at_the_open_incident():
    ledger, client = store(window(600))
    ledger.accept(event(track_recovery=True), 30, 15)
    state = next(
        r["Put"]["Item"]
        for r in written(client)[1]
        if "Put" in r and r["Put"]["Item"]["PK"]["S"].startswith("ALARM#")
    )
    assert state["last_incident_id"]["S"] == PARENT


def conflict():
    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "x"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
        },
        "TransactWriteItems",
    )


def test_a_lost_race_on_the_window_is_decided_again_and_a_real_duplicate_is_not():
    ledger, client = store()
    markers = iter([None, window(600)])  # first read: no window; by the retry another writer opened one
    ledger.table.get_item.side_effect = lambda Key, ConsistentRead=False: {
        "Item": next(markers) if Key["PK"].startswith("COOLDOWN#") else None
    }
    client.transact_write_items.side_effect = [conflict(), None]
    assert ledger.accept(event(), 30, 15) == "SUPPRESSED"
    assert client.transact_write_items.call_count == 2

    ledger, client = store()
    client.transact_write_items.side_effect = conflict()
    ledger.table.get_item.side_effect = lambda Key, ConsistentRead=False: {
        "Item": {"PK": "EVENT#x"} if Key["PK"].startswith("EVENT#") else None
    }
    assert ledger.accept(event(), 30, 15) == "DUPLICATE"
    assert client.transact_write_items.call_count == 1


def test_a_persistent_conflict_is_raised_for_the_queue_to_retry():
    ledger, client = store()
    client.transact_write_items.side_effect = conflict()
    with pytest.raises(ClientError):
        ledger.accept(event(), 30, 15)
    assert client.transact_write_items.call_count == 3


def test_ingress_reads_the_setting_and_counts_a_suppressed_alarm(monkeypatch):
    for name, value in {
        "INCIDENT_TABLE": "t",
        "MONITOR_REGION": REGION,
        "ALLOWED_INSTANCE_IDS": IID,
        "ALARMS_TOPIC_ARN": TOPIC,
        "EXPECTED_ACCOUNT_ID": ACCOUNT,
        "ALARM_NAME_PREFIX": "tracaus-staging-",
        "INCIDENT_RETENTION_DAYS": "30",
    }.items():
        monkeypatch.setenv(name, value)
    fake = MagicMock()
    fake.accept.return_value = "SUPPRESSED"
    monkeypatch.setattr(pipeline, "ledger", lambda: fake)
    body = json.dumps(
        {
            "Type": "Notification",
            "TopicArn": TOPIC,
            "Message": json.dumps(
                {
                    "AlarmName": f"tracaus-staging-{IID}-cpu",
                    "AlarmArn": f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:tracaus-staging-{IID}-cpu",
                    "StateChangeTime": "2026-10-09T10:00:00Z",
                    "NewStateValue": "ALARM",
                    "Trigger": {"Dimensions": [{"name": "InstanceId", "value": IID}]},
                }
            ),
        }
    )
    records = {
        "Records": [
            {"messageId": "m", "body": body, "attributes": {"SentTimestamp": str(int(time.time() * 1000))}}
        ]
    }
    assert pipeline.ingest(records) == {"batchItemFailures": []}
    assert fake.accept.call_args.args[2] == 15  # default
    monkeypatch.setenv("INCIDENT_COOLDOWN_MINUTES", "40")
    pipeline.ingest(records)
    assert fake.accept.call_args.args[2] == 40


@pytest.mark.parametrize("value", [-1, 121, True, "15", 1.5])
def test_the_setting_is_a_whole_number_of_minutes_up_to_120(tmp_path, value):
    spec = load(ROOT / "examples/deployment.example.json")
    base = json.loads((ROOT / "examples/durable.example.json").read_text())
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({**base, "incident_cooldown_minutes": value}))
    with pytest.raises(ValueError, match="incident_cooldown_minutes"):
        durable.load_config(path, spec)
    for good in (0, 15, 120):
        path.write_text(json.dumps({**base, "incident_cooldown_minutes": good}))
        assert durable.load_config(path, spec)["incident_cooldown_minutes"] == good


def test_only_a_configured_value_reaches_the_ingress_function():
    spec = load(ROOT / "examples/deployment.example.json")
    base = json.loads((ROOT / "examples/durable.example.json").read_text())

    def ingress_env(config):
        function = validate_durable.examples(spec, config)["durable-runtime"]["Resources"]["Ingress"]
        return function["Properties"]["Environment"]["Variables"]

    assert "INCIDENT_COOLDOWN_MINUTES" not in ingress_env(base)  # unset: the code default applies
    assert ingress_env({**base, "incident_cooldown_minutes": 30})["INCIDENT_COOLDOWN_MINUTES"] == "30"
