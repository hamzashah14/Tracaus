"""Fault and identity checks without credentials or network access."""

import hashlib
import json
from decimal import Decimal
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from tracaus import pipeline
from tracaus.incident import InvalidEvent, normalize_sns
from tracaus.ledger import Ledger
from tracaus.status import load as load_status

ACCOUNT = "123456789012"
REGION = "eu-central-1"
IID = "i-0123456789abcdef0"
TOPIC = f"arn:aws:sns:{REGION}:{ACCOUNT}:tracaus-staging-alarms"
ALARM = {
    "AlarmName": f"tracaus-staging-{IID}-status",
    "AlarmArn": f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:tracaus-staging-{IID}-status",
    "StateChangeTime": "2026-10-05T10:00:00Z",
    "NewStateValue": "ALARM",
    "Trigger": {"Dimensions": [{"name": "InstanceId", "value": IID}]},
}


def envelope(value=ALARM, topic=TOPIC):
    return json.dumps({"Type": "Notification", "TopicArn": topic, "Message": json.dumps(value)})


def normalize(raw):
    return normalize_sns(raw, TOPIC, ACCOUNT, REGION, {IID}, "tracaus-staging-")


def failure(code):
    response = {"Error": {"Code": code, "Message": "synthetic"}}
    if code == "TransactionCanceledException":
        response["CancellationReasons"] = [{"Code": "ConditionalCheckFailed"}]
    return ClientError(response, "TransactWriteItems")


def test_native_transition_identity_and_recovery_are_distinct():
    alarm = normalize(envelope())
    duplicate = normalize(envelope())
    recovery = normalize(envelope({**ALARM, "NewStateValue": "OK"}))
    later = normalize(envelope({**ALARM, "StateChangeTime": "2026-10-05T10:05:00Z"}))
    assert alarm["event_id"] == duplicate["event_id"]
    assert len({alarm["event_id"], recovery["event_id"], later["event_id"]}) == 3
    assert alarm["correlation_key"] == recovery["correlation_key"]
    assert recovery["actionable"] is False


@pytest.mark.parametrize(
    "raw",
    [
        envelope(topic=f"arn:aws:sns:{REGION}:{ACCOUNT}:other"),
        envelope({**ALARM, "AlarmArn": "arn:aws:cloudwatch:us-east-1:123456789012:alarm:other"}),
        envelope(
            {**ALARM, "Trigger": {"Dimensions": [{"name": "InstanceId", "value": "i-11111111111111111"}]}}
        ),
        envelope({**ALARM, "StateChangeTime": "bad"}),
        "not-json",
    ],
)
def test_unsafe_or_malformed_source_reaches_redrive_not_database(raw):
    with pytest.raises(InvalidEvent):
        normalize(raw)


def test_accepted_event_and_initial_work_commit_in_one_transaction():
    client, table = MagicMock(), MagicMock()
    store = Ledger("test", client, table)
    event = normalize(envelope())
    assert store.accept(event, 30) == "ACCEPTED"
    records = client.transact_write_items.call_args.kwargs["TransactItems"]
    assert len(records) == 5
    assert all("ConditionExpression" in record["Put"] for record in records)
    assert event["original_event"] == json.dumps(ALARM)
    client.transact_write_items.side_effect = failure("TransactionCanceledException")
    table.get_item.return_value = {"Item": {"PK": f"EVENT#{event['event_id']}"}}
    assert store.accept(event, 30) == "DUPLICATE"
    table.get_item.return_value = {}
    with pytest.raises(ClientError):
        store.accept(event, 30)  # Failed transaction without durable event is never acknowledged.


def test_transaction_capacity_failure_is_not_mistaken_for_stale_work():
    client = MagicMock()
    store = Ledger("test", client, MagicMock())
    client.transact_write_items.side_effect = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "synthetic"},
            "CancellationReasons": [{"Code": "ProvisionedThroughputExceeded"}],
        },
        "TransactWriteItems",
    )
    claim = {"PK": "INCIDENT#" + "a" * 32, "ttl": 1000, "lease_owner": "x", "fencing_token": 1}
    with pytest.raises(ClientError):
        store.complete(
            claim,
            {"bucket": "b", "key": "k", "sha256": "a" * 64, "classification": "redacted"},
            "v1",
            "COMPLETE",
            101,
        )


def test_recovery_transition_retained_without_model_or_notification():
    client = MagicMock()
    store = Ledger("test", client, MagicMock())
    assert store.accept(normalize(envelope({**ALARM, "NewStateValue": "OK"})), 30) == "NON_ACTIONABLE"
    assert len(client.transact_write_items.call_args.kwargs["TransactItems"]) == 1


def test_dispatch_send_failure_leaves_pending_intent():
    table = MagicMock()
    store = Ledger("test", MagicMock(), table)
    sqs = MagicMock()
    sqs.send_message.side_effect = RuntimeError("synthetic outage")
    row = Ledger.intent("a" * 32, "INITIAL", "2026-10-05T10:00:00Z", 100)
    with pytest.raises(RuntimeError):
        store.dispatch(row, sqs, "queue-url")
    table.update_item.assert_not_called()
    sqs.send_message.side_effect = None
    assert store.dispatch(row, sqs, "queue-url") == "SENT"
    table.update_item.assert_called_once()


def test_concurrent_claim_and_late_completion_are_fenced():
    table, client = MagicMock(), MagicMock()
    store = Ledger("test", client, table)
    table.get_item.return_value = {
        "Item": {
            "PK": "INCIDENT#" + "a" * 32,
            "status": "PENDING",
            "fencing_token": 1,
            "attempts": 1,
            "deadline_epoch": 1000,
            "ttl": 1000,
        }
    }
    client.transact_write_items.side_effect = failure("TransactionCanceledException")
    assert store.claim("a" * 32, "one", 100, 20) is None
    client.transact_write_items.side_effect = None
    claim = store.claim("a" * 32, "two", 121, 20)
    assert claim["fencing_token"] == 2
    client.transact_write_items.side_effect = failure("TransactionCanceledException")
    result = store.complete(
        claim,
        {"bucket": "bucket", "key": "key", "sha256": "a" * 64, "classification": "redacted"},
        "version",
        "COMPLETE",
        125,
    )
    assert result == "STALE"
    condition = client.transact_write_items.call_args.kwargs["TransactItems"][1]["Update"][
        "ConditionExpression"
    ]
    assert "fencing_token=:token" in condition and "lease_until>=:now" in condition


def test_independent_initial_notification_does_not_call_bedrock(monkeypatch):
    values = {
        "INCIDENT_TABLE": "test",
        "REPORTS_TOPIC_ARN": "arn:aws:sns:x:1:reports",
        "STATUS_BASE_URL": "https://customer.example/incidents",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    iid = "a" * 32
    store, sns = MagicMock(), MagicMock()
    store.get.side_effect = lambda pk, sk="META": (
        {"status": "PENDING", "attempts": 0}
        if sk.startswith("NOTIFICATION#")
        else {"instance_id": IID, "occurred_at": "2026-10-05T10:00:00Z", "status": "PENDING"}
    )
    store.claim_notification.return_value = {
        "PK": f"INCIDENT#{iid}",
        "SK": "NOTIFICATION#INITIAL",
        "lease_owner": "owner",
        "fencing_token": 1,
        "attempts": 1,
    }
    sns.publish.return_value = {"MessageId": "synthetic-ack"}
    with (
        patch.object(pipeline, "ledger", return_value=store),
        patch.object(pipeline, "clients", return_value=sns),
    ):
        response = pipeline.initial(
            {"Records": [{"messageId": "record-1", "body": f"INCIDENT#{iid}|INTENT#INITIAL#1"}]}
        )
    assert response == {"batchItemFailures": []}
    assert "Source context" not in sns.publish.call_args.kwargs["Message"]
    assert f"?incident={iid}" in sns.publish.call_args.kwargs["Message"]
    store.notification_result.assert_called_once()


def test_publisher_failure_retries_only_failed_record(monkeypatch):
    for key, value in {
        "INCIDENT_TABLE": "test",
        "REPORTS_TOPIC_ARN": "arn:aws:sns:x:1:reports",
        "STATUS_BASE_URL": "https://customer.example/",
    }.items():
        monkeypatch.setenv(key, value)
    store, sns = MagicMock(), MagicMock()
    store.get.side_effect = lambda pk, sk="META": (
        {"status": "PENDING", "attempts": 0}
        if sk.startswith("NOTIFICATION#")
        else {"instance_id": IID, "occurred_at": "2026-10-05T10:00:00Z", "status": "PENDING"}
    )
    store.claim_notification.return_value = {
        "PK": "INCIDENT#" + "a" * 32,
        "SK": "NOTIFICATION#INITIAL",
        "lease_owner": "owner",
        "fencing_token": 1,
        "attempts": 1,
    }
    sns.publish.side_effect = [RuntimeError("publish unavailable"), {"MessageId": "accepted"}]
    records = [{"messageId": key, "body": f"INCIDENT#{'a' * 32}|INTENT#INITIAL#1"} for key in ("bad", "good")]
    with (
        patch.object(pipeline, "ledger", return_value=store),
        patch.object(pipeline, "clients", return_value=sns),
    ):
        result = pipeline.initial({"Records": records})
    assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}
    assert store.notification_result.call_args_list[0].args[1] == "PENDING"
    assert store.notification_result.call_args_list[1].args[1] == "PUBLISHER_ACCEPTED"


def test_reconciler_resends_pending_and_fences_expired_lease(monkeypatch):
    monkeypatch.setenv("INITIAL_QUEUE_URL", "initial")
    monkeypatch.setenv("WORK_QUEUE_URL", "work")
    monkeypatch.setenv("REPORT_QUEUE_URL", "report")
    store, sqs = MagicMock(), MagicMock()
    store.dispatch.return_value = "SENT"
    store.sweep_state.return_value = {"revision": 0, "cursor": None}
    store.save_sweep.return_value = "SAVED"
    intent = Ledger.intent("a" * 32, "INITIAL", "2026-10-05T10:00:00Z", 1000)
    store.pending.return_value = ([intent], None)
    store.expired.return_value = (
        [
            {
                "PK": "INCIDENT#" + "b" * 32,
                "SK": "META",
                "GSI2PK": "ACTIVE",
                "GSI2SK": "000000000000#" + "b" * 32,
            }
        ],
        None,
    )
    store.overdue.return_value = ([], None)
    store.get.return_value = {
        "PK": "INCIDENT#" + "b" * 32,
        "status": "RUNNING",
        "lease_until": 0,
        "attempts": 1,
        "fencing_token": 1,
    }
    with (
        patch.object(pipeline, "ledger", return_value=store),
        patch.object(pipeline, "clients", return_value=sqs),
    ):
        result = pipeline.reconcile({"sweep": "pending"})
        expired = pipeline.reconcile({"sweep": "expired"})
    assert result == {"sweep": "pending", "checked": 1}
    assert expired == {"sweep": "expired", "checked": 1}
    store.dispatch.assert_called_once()
    store.recover.assert_called_once()


def test_queued_deadline_creates_report_notification_atomically():
    client = MagicMock()
    store = Ledger("test", client, MagicMock())
    incident = {
        "PK": "INCIDENT#" + "a" * 32,
        "status": "PENDING",
        "deadline_epoch": 100,
        "fencing_token": 0,
        "ttl": 1000,
    }
    assert store.degrade_overdue(incident, 101) == "DEGRADED"
    records = client.transact_write_items.call_args.kwargs["TransactItems"]
    assert len(records) == 3
    assert "deadline_epoch<=:now" in records[0]["Update"]["ConditionExpression"]
    assert records[1]["Put"]["Item"]["SK"]["S"] == "NOTIFICATION#REPORT"
    assert records[2]["Put"]["Item"]["SK"]["S"] == "INTENT#REPORT#overdue"
    client.transact_write_items.side_effect = failure("TransactionCanceledException")
    assert store.degrade_overdue(incident, 101) == "STALE"


def test_model_retry_commits_new_work_intent_with_state():
    client = MagicMock()
    store = Ledger("test", client, MagicMock())
    claim = {
        "PK": "INCIDENT#" + "a" * 32,
        "lease_owner": "worker",
        "fencing_token": 1,
        "deadline_epoch": 1000,
        "attempts": 1,
        "ttl": 2000,
    }
    assert store.retry(claim) == "RETRY"
    records = client.transact_write_items.call_args.kwargs["TransactItems"]
    assert len(records) == 3
    assert "fencing_token=:token" in records[1]["Update"]["ConditionExpression"]
    assert records[2]["Put"]["Item"]["SK"]["S"] == "INTENT#WORK#2"
    client.transact_write_items.side_effect = failure("TransactionCanceledException")
    assert store.retry(claim) == "STALE"


def test_reconciler_marks_queued_overdue_incident(monkeypatch):
    for key in ("WORK_QUEUE_URL", "INITIAL_QUEUE_URL", "REPORT_QUEUE_URL"):
        monkeypatch.setenv(key, key.lower())
    store, sqs = MagicMock(), MagicMock()
    store.pending.return_value = ([], None)
    store.expired.return_value = ([], None)
    store.sweep_state.return_value = {"revision": 0, "cursor": None}
    store.save_sweep.return_value = "SAVED"
    store.overdue.return_value = (
        [
            {
                "PK": "INCIDENT#" + "a" * 32,
                "SK": "META",
                "GSI3PK": "OPEN",
                "GSI3SK": "000000000000#" + "a" * 32,
            }
        ],
        None,
    )
    store.get.return_value = {"PK": "INCIDENT#" + "a" * 32, "status": "PENDING"}
    with (
        patch.object(pipeline, "ledger", return_value=store),
        patch.object(pipeline, "clients", return_value=sqs),
    ):
        result = pipeline.reconcile({"sweep": "overdue"})
    assert result == {"sweep": "overdue", "checked": 1}
    store.degrade_overdue.assert_called_once()


def test_model_work_refuses_incident_with_insufficient_remaining_deadline(monkeypatch):
    for key, value in {
        "REPORT_BUCKET": "synthetic-report-bucket",
        "REPORT_KMS_KEY_ARN": "arn:aws:kms:eu-central-1:123456789012:key/synthetic",
    }.items():
        monkeypatch.setenv(key, value)
    store, s3 = MagicMock(), MagicMock()
    store.get.return_value = {"status": "PENDING", "deadline_epoch": 1, "work_intent_sk": "INTENT#WORK#1"}
    with (
        patch.object(pipeline, "ledger", return_value=store),
        patch.object(pipeline, "clients", return_value=s3),
    ):
        result = pipeline.work(
            {"Records": [{"messageId": "expired", "body": f"INCIDENT#{'a' * 32}|INTENT#WORK#1"}]},
            None,
        )
    assert result == {"batchItemFailures": [{"itemIdentifier": "expired"}]}
    store.claim.assert_not_called()


def test_replay_rejects_terminal_work_and_records_reviewed_intent():
    client, table = MagicMock(), MagicMock()
    store = Ledger("test", client, table)
    event_id = "a" * 64
    iid = "a" * 32
    event = {"incident_id": iid, "ttl": 1000}
    incident = {"status": "COMPLETE", "attempts": Decimal(1), "fencing_token": Decimal(1)}
    table.get_item.side_effect = lambda Key, ConsistentRead: {
        "Item": event if Key["PK"].startswith("EVENT#") else incident
    }
    with pytest.raises(ValueError, match="cannot be replayed"):
        store.replay_plan(event_id)
    incident["status"] = "RETRY"
    reviewed = store.replay_plan(event_id)
    assert reviewed["attempts"] == 1 and len(reviewed["review_hash"]) == 64
    with pytest.raises(ValueError, match="changed"):
        store.replay({**reviewed, "attempts": 0}, "operator")
    result = store.replay(reviewed, "operator")
    assert result["status"] == "REPLAY_INTENT_RECORDED"
    records = client.transact_write_items.call_args.kwargs["TransactItems"]
    assert len(records) == 3 and "fencing_token=:token" in records[0]["Update"]["ConditionExpression"]


def test_status_lookup_rejects_untrusted_id_before_client():
    with patch("boto3.resource") as client:
        with pytest.raises(ValueError, match="Invalid incident"):
            load_status("../../other-tenant")
    client.assert_not_called()


def test_durable_templates_keep_capture_ahead_of_cutover_and_scope_roles():
    from infra import durable_templates
    from infra.spec import ROOT, load

    spec = load(ROOT / "examples/deployment.example.json")
    config = json.loads((ROOT / "examples/durable.example.json").read_text())
    foundation = durable_templates.foundation(spec, config)["Resources"]
    assert foundation["IngressSubscription"]["DependsOn"] == ["IngressPolicy", "DeliveryDeadPolicy"]
    assert foundation["Incidents"]["Properties"]["StreamSpecification"]["StreamViewType"] == "KEYS_ONLY"
    assert foundation["Evidence"]["Properties"]["VersioningConfiguration"]["Status"] == "Enabled"
    outputs = {
        key + "QueueArn": durable_templates.queue_arn(spec, key)
        for key in ("Ingress", "Work", "Initial", "Report")
    }
    outputs.update(
        StreamArn="arn:aws:dynamodb:eu-central-1:123456789012:table/test/stream/1",
        StreamDeadArn="arn:aws:sqs:eu-central-1:123456789012:stream-dead",
        DeliveryDeadArn="arn:aws:sqs:eu-central-1:123456789012:delivery-dead",
        TableArn="arn:aws:dynamodb:eu-central-1:123456789012:table/test",
        EvidenceBucket="example-reports",
        EvidenceKeyArn="arn:aws:kms:eu-central-1:123456789012:key/example",
    )
    versions = {
        name + "VersionArn": f"arn:aws:lambda:eu-central-1:123456789012:function:{name}:1"
        for name in ("Ingress", "Dispatch", "Investigate", "Initial", "Report", "Reconcile")
    }
    active = durable_templates.active_routing(
        spec,
        outputs,
        versions,
    )["Resources"]
    assert "WorkerSubscription" not in active and "IngressSubscription" not in active
    assert active["InitialMapping"]["Properties"]["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
    assert active["WorkMapping"]["Properties"]["BatchSize"] == 1
    assert active["WorkMapping"]["Properties"]["ScalingConfig"] == {"MaximumConcurrency": 2}
    paused = durable_templates.active_routing(
        spec,
        outputs,
        versions,
        True,
    )["Resources"]
    assert paused["WorkMapping"]["Properties"]["Enabled"] is False
    assert paused["InitialMapping"]["Properties"]["Enabled"] is True
    assert paused["SweepRule"]["Properties"]["State"] == "ENABLED"


def test_synthetic_durable_cloud_bundle_rejected_before_credentials(tmp_path, monkeypatch):
    from infra import durable_ops
    from infra.durable import render
    from infra.spec import ROOT

    bundle = render(
        ROOT / "examples/deployment.example.json", ROOT / "examples/durable.example.json", tmp_path
    )
    with patch.object(durable_ops, "clients") as cloud:
        monkeypatch.setattr(
            "sys.argv",
            [
                "durable_ops",
                "inspect",
                "--bundle",
                str(tmp_path),
                "--review-hash",
                bundle["review_hash"],
                "--output",
                str(tmp_path / "output.json"),
            ],
        )
        assert durable_ops.main() == 1
    cloud.assert_not_called()


def test_thousand_unique_transitions_and_duplicate_delivery_keep_distinct_incidents():
    unique = []
    for index in range(1000):
        value = {
            **ALARM,
            "StateChangeTime": f"2026-10-05T{index // 3600:02d}:{(index // 60) % 60:02d}:{index % 60:02d}Z",
        }
        unique.append(normalize(envelope(value)))
    repeated = [
        normalize(envelope({**ALARM, "StateChangeTime": unique[index]["occurred_at"]}))
        for index in range(0, 1000, 5)
    ]
    assert len({value["event_id"] for value in unique}) == 1000
    assert {value["event_id"] for value in repeated} <= {value["event_id"] for value in unique}
    assert len({value["correlation_key"] for value in unique}) == 1


def test_stream_failure_retries_failed_and_later_records(monkeypatch):
    for key in ("WORK_QUEUE_URL", "INITIAL_QUEUE_URL", "REPORT_QUEUE_URL"):
        monkeypatch.setenv(key, key.lower())
    store, sqs = MagicMock(), MagicMock()
    store.get.return_value = {
        "PK": "INCIDENT#" + "a" * 32,
        "SK": "INTENT#WORK#1",
        "status": "PENDING",
        "kind": "WORK",
    }
    store.dispatch.side_effect = RuntimeError("send failed")
    events = [
        {
            "dynamodb": {
                "SequenceNumber": str(i),
                "Keys": {"PK": {"S": "INCIDENT#" + "a" * 32}, "SK": {"S": "INTENT#WORK#1"}},
            }
        }
        for i in range(3)
    ]
    with (
        patch.object(pipeline, "ledger", return_value=store),
        patch.object(pipeline, "clients", return_value=sqs),
    ):
        assert pipeline.dispatch({"Records": events}) == {"batchItemFailures": [{"itemIdentifier": "0"}]}
    store.dispatch.assert_called_once()


def test_exhausted_notification_is_failed_and_remains_recoverable(monkeypatch):
    for key, value in {
        "INCIDENT_TABLE": "test",
        "REPORTS_TOPIC_ARN": "arn:aws:sns:x:1:reports",
        "STATUS_BASE_URL": "https://customer.example/",
    }.items():
        monkeypatch.setenv(key, value)
    iid = "a" * 32
    store, sns = MagicMock(), MagicMock()
    store.get.side_effect = lambda pk, sk="META": (
        {"status": "PENDING", "attempts": 2}
        if sk.startswith("NOTIFICATION#")
        else {"instance_id": IID, "occurred_at": "2026-10-05T10:00:00Z", "status": "PENDING"}
    )
    store.claim_notification.return_value = {
        "PK": f"INCIDENT#{iid}",
        "SK": "NOTIFICATION#INITIAL",
        "lease_owner": "owner",
        "fencing_token": 3,
        "attempts": 3,
    }
    sns.publish.side_effect = RuntimeError("SNS unavailable")
    with (
        patch.object(pipeline, "ledger", return_value=store),
        patch.object(pipeline, "clients", return_value=sns),
    ):
        result = pipeline.initial(
            {"Records": [{"messageId": "id", "body": f"INCIDENT#{iid}|INTENT#INITIAL#1"}]}
        )
    assert result == {"batchItemFailures": [{"itemIdentifier": "id"}]}
    assert store.notification_result.call_args.args[1] == "FAILED"


def test_attempt_record_and_incident_claim_commit_together():
    client, table = MagicMock(), MagicMock()
    table.get_item.return_value = {
        "Item": {
            "PK": "INCIDENT#" + "a" * 32,
            "status": "PENDING",
            "fencing_token": 0,
            "attempts": 0,
            "deadline_epoch": 1000,
            "ttl": 2000,
        }
    }
    store = Ledger("test", client, table)
    assert store.claim("a" * 32, "owner", 100, 50)["attempts"] == 1
    records = client.transact_write_items.call_args.kwargs["TransactItems"]
    assert len(records) == 2 and records[1]["Put"]["Item"]["SK"]["S"] == "ATTEMPT#1"
    assert "fencing_token=:previous" in records[0]["Update"]["ConditionExpression"]


@pytest.mark.parametrize(
    "attempts,deadline,expected", [(1, 1000, "RETRY"), (3, 1000, "DEGRADED"), (1, 100, "DEGRADED")]
)
def test_killed_attempt_is_closed_and_handed_off_without_worker_finally(attempts, deadline, expected):
    client = MagicMock()
    store = Ledger("test", client, MagicMock())
    claim = {
        "PK": "INCIDENT#" + "a" * 32,
        "status": "RUNNING",
        "lease_until": 90,
        "lease_owner": "dead-worker",
        "fencing_token": attempts,
        "attempts": attempts,
        "deadline_epoch": deadline,
        "ttl": 2000,
    }
    assert store.recover(claim, 101) == expected
    records = client.transact_write_items.call_args.kwargs["TransactItems"]
    assert records[0]["Update"]["ExpressionAttributeValues"][":next"]["S"] == "EXPIRED"
    assert "lease_until<:now" in records[1]["Update"]["ConditionExpression"]
    assert records[2]["Put"]["Item"]["kind"]["S"] == ("WORK" if expected == "RETRY" else "REPORT")


def test_checkpoint_reference_is_fenced_and_atomic():
    client = MagicMock()
    store = Ledger("test", client, MagicMock())
    claim = {"PK": "INCIDENT#" + "a" * 32, "ttl": 1000, "lease_owner": "owner", "fencing_token": 1}
    evidence = {"bucket": "b", "key": "k", "sha256": "a" * 64, "classification": "partial"}
    assert store.checkpoint(claim, evidence, "v1", 100) == "CHECKPOINTED"
    records = client.transact_write_items.call_args.kwargs["TransactItems"]
    assert len(records) == 3
    assert "lease_until>=:now" in records[0]["Update"]["ConditionExpression"]
    client.transact_write_items.side_effect = failure("TransactionCanceledException")
    assert store.checkpoint(claim, evidence, "v2", 101) == "STALE"


def test_status_reads_only_pinned_version_checks_hash_and_marks_partial(monkeypatch):
    monkeypatch.setenv("INCIDENT_TABLE", "test")
    monkeypatch.setenv("MONITOR_REGION", REGION)
    monkeypatch.setenv("REPORT_BUCKET", "private-bucket")
    iid, data = "a" * 32, b"partial evidence"
    table, s3, resource = MagicMock(), MagicMock(), MagicMock()
    resource.Table.return_value = table
    table.get_item.side_effect = [
        {"Item": {"status": "RUNNING", "checkpoint_version": "v1", "ttl": 4102444800}},
        {
            "Item": {
                "bucket": "private-bucket",
                "key": f"incidents/{iid}/checkpoints/1.txt",
                "version_id": "v1",
                "sha256": hashlib.sha256(data).hexdigest(),
                "ttl": 4102444800,
            }
        },
    ]
    body = BytesIO(data)
    s3.get_object.return_value = {"Body": body}
    with patch("boto3.resource", return_value=resource), patch("boto3.client", return_value=s3):
        result = load_status(iid)
    assert result["partial"] is True and result["report"] == data.decode()
    assert s3.get_object.call_args.kwargs["VersionId"] == "v1" and body.closed


def test_retry_backoff_is_not_sent_early():
    table, client = MagicMock(), MagicMock()
    store = Ledger("test", client, table)
    row = Ledger.intent("a" * 32, "WORK", "2099-01-01T00:00:00Z", 1000)
    sqs = MagicMock()
    assert store.dispatch(row, sqs, "queue") == "DEFERRED"
    sqs.send_message.assert_not_called()


def test_reconciler_continues_after_individual_dispatch_failure(monkeypatch):
    for key in ("WORK_QUEUE_URL", "INITIAL_QUEUE_URL", "REPORT_QUEUE_URL"):
        monkeypatch.setenv(key, key.lower())
    store, sqs = MagicMock(), MagicMock()
    store.sweep_state.return_value = {"revision": 0, "cursor": None}
    store.save_sweep.return_value = "SAVED"
    store.expired.return_value = ([], None)
    store.overdue.return_value = ([], None)
    store.pending.side_effect = [
        ([Ledger.intent("a" * 32, "WORK", "2026-01-01T00:00:00Z", 1000)], {"cursor": "one"}),
        ([Ledger.intent("b" * 32, "INITIAL", "2026-01-01T00:00:00Z", 1000)], None),
    ]
    store.dispatch.side_effect = [RuntimeError("queue unavailable"), "SENT"]
    with (
        patch.object(pipeline, "ledger", return_value=store),
        patch.object(pipeline, "clients", return_value=sqs),
    ):
        with pytest.raises(RuntimeError, match="Reconciliation incomplete"):
            pipeline.reconcile()
    assert store.dispatch.call_count == 2 and store.pending.call_count == 2


@pytest.mark.parametrize("boundary", ["model", "object", "commit"])
def test_worker_failure_keeps_failed_handoff_recoverable(monkeypatch, boundary):
    import time

    monkeypatch.setenv("REPORT_BUCKET", "synthetic-bucket")
    monkeypatch.setenv("REPORT_KMS_KEY_ARN", "synthetic-key")
    store, s3 = MagicMock(), MagicMock()
    iid = "a" * 32
    current = {
        "PK": f"INCIDENT#{iid}",
        "status": "PENDING",
        "deadline_epoch": int(time.time()) + 600,
        "work_intent_sk": "INTENT#WORK#1",
    }
    claim = {
        **current,
        "status": "RUNNING",
        "lease_until": int(time.time()) + 450,
        "fencing_token": 1,
        "attempts": 1,
        "lease_owner": "owner",
        "event_id": "a" * 64,
        "ttl": 2000,
    }
    store.get.side_effect = [current, {"event": normalize(envelope())}]
    store.claim.return_value = claim
    model = MagicMock(return_value=("completed investigation", True))
    s3.put_object.return_value = {"VersionId": "v1"}
    store.complete.return_value = "COMPLETED"
    if boundary == "model":
        model.side_effect = RuntimeError("model unavailable")
    elif boundary == "object":
        s3.put_object.side_effect = RuntimeError("evidence denied")
    else:
        store.complete.side_effect = RuntimeError("commit unavailable")
    with (
        patch.object(pipeline, "ledger", return_value=store),
        patch.object(pipeline, "clients", return_value=s3),
        patch.object(pipeline, "invoke_agent", model),
    ):
        result = pipeline.work(
            {"Records": [{"messageId": "one", "body": f"INCIDENT#{iid}|INTENT#WORK#1"}]}, None
        )
    if boundary == "model":
        assert result == {"batchItemFailures": []}
        store.retry.assert_called_once()
        store.complete.assert_not_called()
    else:
        assert result == {"batchItemFailures": [{"itemIdentifier": "one"}]}
        store.retry.assert_not_called()  # Expired lease + reconciler provide the next durable handoff.


def test_ambiguous_publication_retries_stable_id_without_model_work(monkeypatch):
    for key, value in {
        "REPORTS_TOPIC_ARN": "synthetic-reports",
        "STATUS_BASE_URL": "https://customer.example/",
    }.items():
        monkeypatch.setenv(key, value)
    store, sns = MagicMock(), MagicMock()
    iid = "a" * 32
    store.get.side_effect = lambda pk, sk="META": (
        {"status": "PENDING", "attempts": 0}
        if sk.startswith("NOTIFICATION")
        else {"instance_id": IID, "status": "COMPLETE", "report_version": "v1"}
    )
    store.claim_notification.return_value = {
        "PK": f"INCIDENT#{iid}",
        "SK": "NOTIFICATION#REPORT",
        "lease_owner": "owner",
        "fencing_token": 1,
        "attempts": 1,
    }
    sns.publish.return_value = {"MessageId": "accepted"}
    store.notification_result.side_effect = [RuntimeError("ack write lost"), None]
    event = {"Records": [{"messageId": "one", "body": f"INCIDENT#{iid}|INTENT#REPORT#1"}]}
    with (
        patch.object(pipeline, "ledger", return_value=store),
        patch.object(pipeline, "clients", return_value=sns),
        patch.object(pipeline, "invoke_agent") as model,
    ):
        assert pipeline.report(event) == {"batchItemFailures": [{"itemIdentifier": "one"}]}
        assert pipeline.report(event) == {"batchItemFailures": []}
    model.assert_not_called()
    assert sns.publish.call_args_list[0].kwargs["Message"] == sns.publish.call_args_list[1].kwargs["Message"]


def test_cloud_mutation_refuses_changed_source_before_aws():
    from infra import durable_ops

    with (
        patch.object(durable_ops.subprocess, "check_output", side_effect=["different\n", ""]),
        patch.object(durable_ops, "clients") as cloud,
    ):
        with pytest.raises(Exception, match="clean reviewed source"):
            durable_ops.upload({"source_sha": "reviewed", "source_dirty": False}, None)
    cloud.assert_not_called()


def test_ingress_queue_delay_does_not_reset_incident_deadline(monkeypatch):
    from datetime import datetime, timezone

    values = {
        "ALLOWED_INSTANCE_IDS": IID,
        "ALARMS_TOPIC_ARN": TOPIC,
        "EXPECTED_ACCOUNT_ID": ACCOUNT,
        "MONITOR_REGION": REGION,
        "ALARM_NAME_PREFIX": "tracaus-staging-",
        "INCIDENT_RETENTION_DAYS": "30",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    sent = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
    store = MagicMock()
    store.accept.return_value = "ACCEPTED"
    with patch.object(pipeline, "ledger", return_value=store):
        result = pipeline.ingest(
            {
                "Records": [
                    {
                        "messageId": "old",
                        "body": envelope(),
                        "attributes": {"SentTimestamp": str(int(sent.timestamp() * 1000))},
                    }
                ]
            }
        )
    assert result == {"batchItemFailures": []}
    assert store.accept.call_args.args[0]["received_at"] == "2026-10-05T10:00:00Z"


def test_superseded_queue_intent_cannot_start_another_model_attempt(monkeypatch):
    monkeypatch.setenv("REPORT_BUCKET", "synthetic-bucket")
    monkeypatch.setenv("REPORT_KMS_KEY_ARN", "synthetic-key")
    store = MagicMock()
    store.get.return_value = {"status": "RETRY", "work_intent_sk": "INTENT#WORK#2"}
    with (
        patch.object(pipeline, "ledger", return_value=store),
        patch.object(pipeline, "clients"),
        patch.object(pipeline, "invoke_agent") as model,
    ):
        result = pipeline.work(
            {"Records": [{"messageId": "old", "body": f"INCIDENT#{'a' * 32}|INTENT#WORK#1"}]}, None
        )
    assert result == {"batchItemFailures": []}
    store.claim.assert_not_called()
    model.assert_not_called()
