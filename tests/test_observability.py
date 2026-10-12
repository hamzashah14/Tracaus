"""observation detection/freshness/delivery faults without customer credentials or network."""

import copy
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from infra import observation_templates, observations
from infra.spec import ROOT, alarm_descriptors, cwagent, load, metric_catalog, name
from infra.verify import PendingConfirmation, VerificationError
from tracaus import observability, probes, telemetry
from tracaus.incident import InvalidEvent, normalize_sns
from tracaus.ledger import Ledger
from tracaus.nginx import access_evidence
from tracaus.observation_config import validate

SPEC = load(ROOT / "examples/observability.example.json")
CONFIG = SPEC["observability"]
IID = SPEC["instances"][0]["id"]
ACCOUNT = SPEC["account_id"]
REGION = SPEC["monitor_region"]
TOPIC = f"arn:aws:sns:{REGION}:{ACCOUNT}:tracaus-staging-alarms"
CANARY = f"arn:aws:sns:{REGION}:{ACCOUNT}:tracaus-staging-canary"
REPORTS = f"arn:aws:sns:{REGION}:{ACCOUNT}:tracaus-staging-reports"
ROUTE = CONFIG["services"][0]["routes"][0]


@pytest.fixture
def environment(monkeypatch):
    values = {
        "OBS_SETTINGS": json.dumps({**CONFIG, "enabled": True}),
        "MONITOR_REGION": REGION,
        "EXPECTED_ACCOUNT_ID": ACCOUNT,
        "INCIDENT_TABLE": "synthetic",
        "CANARY_TOPIC_ARN": CANARY,
        "ALARMS_TOPIC_ARN": TOPIC,
        "ALARM_NAME_PREFIX": "tracaus-staging-",
        "REPORTS_TOPIC_ARN": REPORTS,
        "PRIMARY_EMAIL": SPEC["notification_email"],
        "FALLBACK_TOPIC_ARN": f"arn:aws:sns:{REGION}:{ACCOUNT}:tracaus-staging-observation-fallback",
        "FALLBACK_EMAIL": "fallback@example.invalid",
        "LOG_GROUP_PREFIX": "/tracaus/staging",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


@pytest.mark.parametrize(
    "url",
    [
        "http://service.invalid/ready",
        "https://user:pass@service.invalid/ready",
        "https://service.invalid/ready?token=secret",
        "https://service.invalid:8443/ready",
        "https://service.invalid/ready#fragment",
    ],
)
def test_probe_urls_reject_credentials_and_unreviewed_protocols(url):
    config = copy.deepcopy(CONFIG)
    config["services"][0]["routes"][0]["url"] = url
    with pytest.raises(ValueError):
        validate(config, SPEC["instances"])


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "::1", "192.168.1.1"])
def test_metadata_private_and_loopback_destinations_denied_before_connect(address):
    connection = MagicMock()
    result = probes.check(
        ROUTE, resolve=lambda *a, **k: [(None, None, None, None, (address, 443))], connection=connection
    )
    assert result["code"] == "DESTINATION_DENIED" and result["healthy"] is False
    connection.assert_not_called()


def test_mixed_dns_answer_denied_and_public_connection_uses_pinned_ip():
    create = MagicMock()

    def answers(*a, **k):
        return [
            (None, None, None, None, ("1.1.1.1", 443)),
            (None, None, None, None, ("10.0.0.1", 443)),
        ]

    assert not probes.check(ROUTE, resolve=answers, connection=create)["healthy"]
    create.assert_not_called()
    create.return_value.getresponse.return_value.status = 200
    create.return_value.getresponse.return_value.read.return_value = b"OK"
    assert probes.check(ROUTE, resolve=lambda *a, **k: answers()[:1], connection=create)["healthy"]
    assert create.call_args.args[0:2] == ("service.example.invalid", "1.1.1.1")
    create.return_value.close.assert_called_once()


@pytest.mark.parametrize("status,body", [(503, b""), (302, b""), (200, b"x" * 4097)])
def test_unhealthy_dependency_redirect_and_large_body_fail_without_following(status, body):
    create = MagicMock()
    create.return_value.getresponse.return_value.status = status
    create.return_value.getresponse.return_value.read.return_value = body
    result = probes.check(
        ROUTE, resolve=lambda *a, **k: [(None, None, None, None, ("1.1.1.1", 443))], connection=create
    )
    assert not result["healthy"] and create.return_value.request.call_count == 1
    create.return_value.close.assert_called_once()


def test_absolute_probe_timeout_kills_and_reaps_child():
    child = MagicMock()
    child.communicate.side_effect = [subprocess.TimeoutExpired("probe", 3), (b"", None)]
    child.poll.return_value = -9
    result = probes.bounded_check(ROUTE, launcher=MagicMock(return_value=child))
    assert result["code"] == "TIMEOUT" and not result["healthy"]
    child.kill.assert_called_once()
    child.stdin.close.assert_called_once()
    child.stdout.close.assert_called_once()


def test_probe_child_rejects_local_ip_without_network():
    result = probes.bounded_check({**ROUTE, "url": "https://127.0.0.1/ready"})
    assert result["code"] == "DESTINATION_DENIED"


@pytest.mark.parametrize("age,healthy", [(0, True), (300, True), (601, False), (-60, False)])
def test_freshness_requires_current_source_timestamp(age, healthy):
    now = datetime(2026, 10, 6, tzinfo=timezone.utc)
    assert probes.fresh([now - timedelta(seconds=age)], now.timestamp(), 600) is healthy
    assert not probes.fresh([], now.timestamp(), 600)


def test_business_traffic_never_substitutes_for_collector_log_heartbeat(environment, monkeypatch):
    now = datetime(2026, 10, 6, tzinfo=timezone.utc)
    service = CONFIG["services"][0]
    cw, logs = MagicMock(), MagicMock()
    cw.get_metric_statistics.return_value = {"Datapoints": [{"Timestamp": now}]}
    logs.get_log_events.return_value = {
        "events": [{"timestamp": int(now.timestamp() * 1000), "message": "healthy business request"}]
    }
    with patch(
        "builtins.open", side_effect=lambda *a, **k: BytesIO(json.dumps(metric_catalog(SPEC)).encode())
    ):
        assert observability.check_freshness(service, CONFIG, cw, logs, now) is False
    beat = {"type": "tracaus.collector-heartbeat", "instance_id": IID, "timestamp": now.isoformat()}
    logs.get_log_events.return_value = {
        "events": [{"timestamp": int(now.timestamp() * 1000), "message": json.dumps(beat)}]
    }
    with patch(
        "builtins.open", side_effect=lambda *a, **k: BytesIO(json.dumps(metric_catalog(SPEC)).encode())
    ):
        assert observability.check_freshness(service, CONFIG, cw, logs, now) is True
        cw.get_metric_statistics.return_value = {"Datapoints": []}
        assert observability.check_freshness(service, CONFIG, cw, logs, now) is False


@pytest.mark.parametrize(
    "status,failed", [(500, True), (502, True), (503, True), (504, True), (200, False), (501, False)]
)
def test_nginx_evidence_uses_status_field_and_normalizes_timezone(status, failed):
    line = f'203.0.113.7 - - [06/Oct/2026:10:15:32 +0500] "GET / HTTP/1.1" {status} 503 "-" "test"'
    evidence = access_evidence(line)
    assert evidence == {"occurred_at": "2026-10-06T05:15:32Z", "status": status, "failed_request": failed}


def test_access_counts_and_diagnostic_events_have_distinct_alarm_descriptors():
    catalog = metric_catalog(SPEC)
    nginx = [d for d in catalog if d["namespace"].endswith("/Nginx")]
    assert {d["metric_name"] for d in nginx} == {
        f"nginx-failed-requests-{IID}",
        f"nginx-diagnostic-events-{IID}",
    }
    manifest = observation_templates.coverage_manifest(SPEC)
    assert len(manifest["alarm_evidence"]) == len(alarm_descriptors(SPEC))
    assert all(d["owner"] and d["descriptor"] for d in manifest["alarm_evidence"])
    assert (
        cwagent(SPEC, SPEC["instances"][0])["logs"]["logs_collected"]["files"]["collect_list"][-1][
            "file_path"
        ]
        == "/var/log/tracaus-collector-heartbeat.log"
    )


def test_canary_isolated_topic_scope_and_no_model_work_intent(environment):
    payload = observability.canary_payload(172800, CONFIG)
    envelope = json.dumps({"Type": "Notification", "TopicArn": CANARY, "Message": json.dumps(payload)})
    source = normalize_sns(envelope, TOPIC, ACCOUNT, REGION, {IID}, "tracaus-staging-", canary_topic=CANARY)
    client, table = MagicMock(), MagicMock()
    assert Ledger("synthetic", client, table).accept(source, 30) == "ACCEPTED"
    records = client.transact_write_items.call_args.kwargs["TransactItems"]
    keys = [r["Put"]["Item"]["SK"]["S"] for r in records]
    assert "INTENT#INITIAL#1" in keys and not any("WORK" in sk for sk in keys)
    assert records[1]["Put"]["Item"]["status"]["S"] == "CANARY"
    with pytest.raises(InvalidEvent):
        normalize_sns(envelope, TOPIC, ACCOUNT, REGION, {IID}, "tracaus-staging-")
    payload["slot"] += 1
    with pytest.raises(InvalidEvent):
        normalize_sns(
            json.dumps({"Type": "Notification", "TopicArn": CANARY, "Message": json.dumps(payload)}),
            TOPIC,
            ACCOUNT,
            REGION,
            {IID},
            "tracaus-staging-",
            canary_topic=CANARY,
        )


def test_expectation_persisted_before_send_and_ambiguous_repeat_reuses_identity(environment):
    ledger, sns = MagicMock(), MagicMock()
    saved = []
    ledger.table.put_item.side_effect = lambda **k: saved.append(k["Item"])
    ledger.get.side_effect = lambda *a: saved[-1]
    sns.publish.return_value = {"MessageId": "synthetic-publisher-id"}
    with (
        patch.object(observability, "store", return_value=ledger),
        patch.object(observability, "clients", return_value=sns),
    ):
        observability.canary_sender()
        payload = json.loads(sns.publish.call_args.kwargs["Message"])
        assert saved[0]["due_epoch"] > saved[0]["created_at"]
        assert payload["slot"] == observability.slot_at(time.time(), CONFIG)
        saved[0]["published_message_id"] = "synthetic-publisher-id"
        ledger.get.side_effect = lambda *a: saved[0]
        assert observability.canary_sender()["status"] == "ALREADY_PUBLISHED"
    assert sns.publish.call_count == 1


def test_recipient_receipt_is_conditional_and_never_reads_notification_body(environment):
    ledger = MagicMock()
    iid = "a" * 32
    ledger.get.return_value = {"canary_slot": 172800, "ttl": time.time() + 3600}
    msg = {
        "Type": "Notification",
        "TopicArn": REPORTS,
        "MessageId": "sns-message",
        "Message": "private body ignored",
        "MessageAttributes": {
            "tracaus_incident": {"Value": iid},
            "tracaus_canary": {"Value": "true"},
            "tracaus_notification": {"Value": iid + "-initial"},
        },
    }
    with patch.object(observability, "store", return_value=ledger):
        assert observability.recipient({"Records": [{"messageId": "q", "body": json.dumps(msg)}]}) == {
            "batchItemFailures": []
        }
    call = ledger.table.update_item.call_args.kwargs
    assert call["ConditionExpression"].startswith("incident_id=:incident AND #t>:now")
    assert call["ExpressionAttributeNames"] == {"#t": "ttl"}  # ttl is a DynamoDB reserved word
    assert "size(recipient_message_ids)<:max" in call["ConditionExpression"]
    assert "if_not_exists(recipient_received_at,:now)" in call["UpdateExpression"]


def test_publish_acceptance_without_recipient_is_not_delivery(environment):
    ledger = MagicMock()
    now = 172800 + 1000
    iid = "a" * 32
    row = {"incident_id": iid, "created_at": 172800, "due_epoch": 172800 + 600}
    ledger.get.side_effect = lambda pk, sk="META": (
        row
        if pk == f"CANARY#{172800}"
        else {"status": "PUBLISHER_ACCEPTED", "publisher_message_id": "sns-message"}
        if sk.startswith("NOTIFICATION")
        else {}
        if pk.startswith("CANARY")
        else {"ttl": now + 100}
    )
    problems, received, age = observability.verify_canary(ledger, CONFIG, now)
    assert "CANARY_RECEIPT_MISSED" in problems and "EMAIL_RECEIPT_UNVERIFIED" in problems and not received
    row.update(recipient_received_at=172900, recipient_message_id="sns-message")
    problems, received, age = observability.verify_canary(ledger, CONFIG, now)
    assert received and "CANARY_RECEIPT_MISSED" not in problems


def test_removed_or_filtered_email_subscription_detected(environment):
    sns = MagicMock()
    sns.get_paginator.return_value.paginate.return_value = [{"Subscriptions": []}]
    assert not observability.confirmed_recipient(sns)
    sns.get_paginator.return_value.paginate.return_value = [
        {
            "Subscriptions": [
                {
                    "Protocol": "email",
                    "Endpoint": SPEC["notification_email"],
                    "SubscriptionArn": REPORTS + ":synthetic",
                }
            ]
        }
    ]
    sns.get_subscription_attributes.return_value = {"Attributes": {"FilterPolicy": '{"exclude":["true"]}'}}
    assert not observability.confirmed_recipient(sns)
    sns.get_subscription_attributes.return_value = {"Attributes": {}}
    assert observability.confirmed_recipient(sns)


def test_maintenance_skips_application_probes_but_preserves_observer_heartbeat(environment, monkeypatch):
    monkeypatch.setenv("MAINTENANCE_MODE", "true")
    ledger, client = MagicMock(), MagicMock()
    ledger.pending.return_value = ([], None)
    with (
        patch.object(observability, "store", return_value=ledger),
        patch.object(observability, "clients", return_value=client),
        patch.object(probes, "bounded_check") as probe,
        patch.object(observability, "emit") as emitted,
    ):
        result = observability.observer()
    probe.assert_not_called()
    client.put_metric_data.assert_not_called()
    emitted.assert_called_once()
    assert emitted.call_args.kwargs["metrics"]["Heartbeat"] == 1 and result["status"] == "CHECKED"


def test_disabled_observers_access_no_cloud(environment, monkeypatch):
    monkeypatch.setenv("OBS_SETTINGS", json.dumps(CONFIG))
    with patch.object(observability, "clients") as client, patch.object(observability, "store") as ledger:
        assert observability.observer()["status"] == "DISABLED"
        assert observability.canary_sender()["status"] == "DISABLED"
    client.assert_not_called()
    ledger.assert_not_called()


def test_structured_metrics_do_not_include_incident_ids_in_dimensions_or_payloads(monkeypatch, capsys):
    monkeypatch.setenv("OBS_NAMESPACE", "tracaus/staging/Pipeline")
    telemetry.emit("work", "COMPLETE", incident_id="a" * 32, fence=2, metrics={"ReportPersisted": 1})
    event = json.loads(capsys.readouterr().out)
    assert event["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["Component"]]
    assert event["incident_id"] == "a" * 32 and "original_event" not in event
    with pytest.raises(ValueError):
        telemetry.emit("work", "password=private", metrics={"UnreviewedMetric": 1})


def test_recovery_links_original_incident_in_atomic_transition_and_fences_source():
    event = {
        "event_id": "b" * 64,
        "incident_id": "b" * 32,
        "instance_id": IID,
        "kind": "alarm",
        "state": "OK",
        "actionable": False,
        "received_at": "2026-10-06T10:00:00Z",
        "occurred_at": "2026-10-06T10:00:00.500000Z",
        "native_id": "synthetic-alarm",
        "track_recovery": True,
    }
    previous = {
        "event_id": "a" * 64,
        "occurred_at": "2026-10-06T10:00:00Z",
        "last_incident_id": "a" * 32,
        "ttl": time.time() + 3600,
    }
    table, client = MagicMock(), MagicMock()
    table.get_item.side_effect = [{"Item": previous}, {"Item": {"ttl": time.time() + 3600}}]
    assert Ledger("synthetic", client, table).accept(event, 30) == "NON_ACTIONABLE"
    records = client.transact_write_items.call_args.kwargs["TransactItems"]
    assert event["related_incident_id"] == "a" * 32
    assert records[1]["Put"]["ConditionExpression"] == "event_id=:previous"
    assert records[2]["Update"]["ExpressionAttributeValues"][":at"]["S"] == event["occurred_at"]


def test_out_of_order_recovery_is_retained_without_overwriting_current_alarm():
    event = {
        "event_id": "b" * 64,
        "incident_id": "b" * 32,
        "instance_id": IID,
        "kind": "alarm",
        "state": "OK",
        "actionable": False,
        "received_at": "2026-10-06T10:00:00Z",
        "occurred_at": "2026-10-06T09:00:00Z",
        "native_id": "synthetic-alarm",
        "track_recovery": True,
    }
    table, client = MagicMock(), MagicMock()
    table.get_item.return_value = {
        "Item": {"event_id": "a" * 64, "occurred_at": "2026-10-06T10:00:00Z", "ttl": time.time() + 3600}
    }
    Ledger("synthetic", client, table).accept(event, 30)
    assert event["out_of_order"] and len(client.transact_write_items.call_args.kwargs["TransactItems"]) == 1


def test_email_attestation_requires_explicit_observed_id_before_aws():
    factory = MagicMock()
    with pytest.raises(VerificationError):
        observations.attest_email({}, factory, "a" * 32 + "-initial")
    factory.assert_not_called()


def test_templates_preserve_cost_pause_and_independent_route():
    from scripts.dev.validate_observations import fixtures

    config = json.loads((ROOT / "examples/durable.example.json").read_text())
    stages = fixtures(SPEC, config)
    active = stages["observations"]["Resources"]
    assert active["ObserverSchedule"]["Properties"]["State"] == "DISABLED"
    assert active["ReceiptMapping"]["Properties"]["Enabled"] is False
    assert active["ObserverHeartbeat"]["Properties"]["TreatMissingData"] == "breaching"
    assert active["ObserverFailure"]["Properties"]["AlarmActions"] == [
        f"arn:aws:sns:{REGION}:{ACCOUNT}:tracaus-staging-observation-fallback",
        REPORTS,
    ]
    assert "bedrock:InvokeModel" not in json.dumps(stages["observation-runtime"])
    assert "lambda:InvokeFunction" not in json.dumps(stages["observation-runtime"])
    assert stages["durable-foundation"]["Resources"]["IngressPolicy"]["Properties"]["PolicyDocument"][
        "Statement"
    ][0]["Condition"]["ArnEquals"]["aws:SourceArn"] == [TOPIC, CANARY]
    observer_env = stages["observation-runtime"]["Resources"]["Observer"]["Properties"]["Environment"][
        "Variables"
    ]
    assert sum(len(k) + len(v) for k, v in observer_env.items()) < 4000


def test_binding_rejects_mutable_or_other_release_observer_before_aws():
    versions = {
        logical
        + "VersionArn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{name(SPEC, function.replace('_', '-'), True)}:1"
        for function, logical in observation_templates.FUNCTIONS.items()
    }
    observations.validate_versions(SPEC, versions)
    versions["ObserverVersionArn"] = versions["ObserverVersionArn"].rsplit(":", 1)[0] + ":$LATEST"
    with pytest.raises(VerificationError):
        observations.validate_versions(SPEC, versions)


def test_correlation_resets_between_requests_and_survives_nested_context(capsys):
    from tracaus.telemetry import correlate, emit

    with correlate("a" * 32, 7):
        emit("tool", "COMPLETE", metrics={"ToolFailure": 0})
        with correlate("b" * 32, 8):
            emit("model", "RESPONSE", metrics={"ModelCalls": 1})
        emit("tool", "PARTIAL", metrics={"ToolNoData": 1})
    emit("model", "RESPONSE", metrics={"ModelCalls": 1})
    lines = [json.loads(s) for s in capsys.readouterr().out.splitlines()]
    assert [(r.get("incident_id"), r.get("fence")) for r in lines] == [
        ("a" * 32, 7),
        ("b" * 32, 8),
        ("a" * 32, 7),
        (None, None),
    ]


def test_canary_schedule_matches_utc_slots_and_invalid_intervals_fail():
    config = json.loads((ROOT / "examples/durable.example.json").read_text())
    from scripts.dev.validate_observations import fixtures

    resources = fixtures(SPEC, config)["observations"]["Resources"]
    assert resources["CanarySchedule"]["Properties"]["ScheduleExpression"] == "cron(0 0 * * ? *)"
    altered = copy.deepcopy(CONFIG)
    altered["canary_interval_minutes"] = 90
    with pytest.raises(ValueError, match="UTC day"):
        validate(altered, SPEC["instances"])


@pytest.mark.parametrize("drift", [None, "schedule", "receipt", "alarm", "filter", "dashboard", "fallback"])
def test_registration_requires_each_independent_component(drift):
    config = json.loads((ROOT / "examples/durable.example.json").read_text())
    from infra.spec import topic_arn
    from scripts.dev.validate_durable import examples

    _, bindings, _, versions = examples(SPEC, config, include_bindings=True)
    observations_versions = {
        logical
        + "VersionArn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{name(SPEC, function.replace('_', '-'), True)}:1"
        for function, logical in observation_templates.FUNCTIONS.items()
    }
    live_spec = {**SPEC, "reference_only": False}
    bundle = {
        "spec": live_spec,
        "config": config,
        "bindings": {**bindings, "versions": versions, "observation_versions": observations_versions},
    }
    resources = observation_templates.active(
        live_spec, bindings["foundation"], observations_versions, versions
    )["Resources"]
    clients = {service: MagicMock() for service in ("sts", "events", "lambda", "cloudwatch", "sns")}
    clients["sts"].get_caller_identity.return_value = {"Account": ACCOUNT}
    schedules = {
        r["Properties"]["Name"]: r["Properties"]
        for r in resources.values()
        if r["Type"] == "AWS::Events::Rule"
    }
    if drift == "schedule":
        next(iter(schedules.values()))["State"] = "ENABLED"
    clients["events"].describe_rule.side_effect = lambda Name: schedules[Name]
    clients["events"].list_targets_by_rule.side_effect = lambda Rule: {"Targets": schedules[Rule]["Targets"]}
    mapping = resources["ReceiptMapping"]["Properties"]
    actual_mapping = {**mapping, "FunctionArn": mapping["FunctionName"], "State": "Disabled"}
    if drift == "receipt":
        actual_mapping["BatchSize"] = 10
    clients["lambda"].get_paginator.return_value.paginate.return_value = [
        {"EventSourceMappings": [actual_mapping]}
    ]
    alarms = [r["Properties"] for r in resources.values() if r["Type"] == "AWS::CloudWatch::Alarm"]
    if drift == "alarm":
        alarms.pop()
    clients["cloudwatch"].describe_alarms.return_value = {"MetricAlarms": alarms}
    clients["cloudwatch"].get_dashboard.return_value = {
        "DashboardBody": "{}"
        if drift == "dashboard"
        else resources["Dashboard"]["Properties"]["DashboardBody"]
    }
    attrs, subscriptions = {}, {}
    for topic, endpoint, policy, dead in (
        ("canary", bindings["foundation"]["IngressQueueArn"], {}, bindings["foundation"]["DeliveryDeadArn"]),
        (
            "reports",
            observation_templates.queue_arn(SPEC, "observation-receipts"),
            {"tracaus_canary": ["true"]},
            observation_templates.queue_arn(SPEC, "observation-dead"),
        ),
        ("observation-fallback", config["fallback_email"], {}, None),
    ):
        arn = topic_arn(SPEC, topic) + ":synthetic-subscription"
        subscriptions[topic_arn(SPEC, topic)] = [
            {
                "Protocol": "email" if dead is None else "sqs",
                "Endpoint": endpoint,
                "SubscriptionArn": "PendingConfirmation" if drift == "fallback" and dead is None else arn,
            }
        ]
        attrs[arn] = {
            "FilterPolicy": json.dumps({} if drift == "filter" and topic == "reports" else policy),
            "RawMessageDelivery": "false",
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": dead}),
        }
    primary_arn = topic_arn(SPEC, "reports") + ":primary-subscription"
    subscriptions[topic_arn(SPEC, "reports")].append(
        {"Protocol": "email", "Endpoint": SPEC["notification_email"], "SubscriptionArn": primary_arn}
    )
    attrs[primary_arn] = {"FilterPolicy": "{}"}
    clients["sns"].get_paginator.return_value.paginate.side_effect = lambda TopicArn: [
        {"Subscriptions": subscriptions[TopicArn]}
    ]
    clients["sns"].get_subscription_attributes.side_effect = lambda SubscriptionArn: {
        "Attributes": attrs[SubscriptionArn]
    }

    def factory(service, region):
        return clients[service]

    if drift:
        with pytest.raises(VerificationError) as failure:
            observations.verify_registration(bundle, factory)
        # Only an unclicked email is a wait; every other drift is a plain failure.
        assert isinstance(failure.value, PendingConfirmation) == (drift == "fallback")
        if drift == "fallback":
            assert failure.value.recipients == ["fallback_email"]
    else:
        assert observations.verify_registration(bundle, factory)["status"] == "PASS"


def test_email_attestation_is_bound_to_current_topic_and_recipient(environment):
    now = 173800
    ledger = MagicMock()
    stamp = {
        "ttl": now + 100,
        "email_received_at": now - 1,
        "recipient_fingerprint": observability.recipient_fingerprint(REPORTS, SPEC["notification_email"]),
    }
    ledger.get.side_effect = lambda pk, sk="META": stamp if pk == "OBS#EMAIL" else {}
    assert "EMAIL_RECEIPT_UNVERIFIED" not in observability.verify_canary(ledger, CONFIG, now)[0]
    stamp["recipient_fingerprint"] = "previous-recipient"
    assert "EMAIL_RECEIPT_UNVERIFIED" in observability.verify_canary(ledger, CONFIG, now)[0]
    stamp["recipient_fingerprint"] = observability.recipient_fingerprint(REPORTS, SPEC["notification_email"])
    stamp["email_received_at"] = now + 60
    assert "EMAIL_RECEIPT_UNVERIFIED" in observability.verify_canary(ledger, CONFIG, now)[0]


def test_future_and_naive_collector_timestamps_are_not_fresh():
    now = datetime.now(timezone.utc)
    assert not probes.fresh([datetime.now()], now.timestamp(), 600)
    assert not probes.fresh([now + timedelta(seconds=10)], now.timestamp(), 600)


def test_complete_coverage_includes_every_rendered_alarm_and_owner():
    from scripts.dev.validate_observations import fixtures

    config = json.loads((ROOT / "examples/durable.example.json").read_text())
    rendered = fixtures(SPEC, config)
    manifest = observation_templates.coverage_manifest(SPEC, rendered.values())
    expected = {
        r["Properties"]["AlarmName"]
        for stage in rendered.values()
        for r in stage["Resources"].values()
        if r["Type"] == "AWS::CloudWatch::Alarm"
    }
    actual = {a["alarm"] for a in manifest["alarm_evidence"] + manifest["operational_alarm_evidence"]}
    assert actual == expected
    assert all(a["owner"] for a in manifest["alarm_evidence"] + manifest["operational_alarm_evidence"])
