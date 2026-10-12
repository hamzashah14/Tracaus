"""Regression gates for reliability controls; synthetic SDKs and loopback only."""

import copy
import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from infra import durable_ops, observations, owned_ops, release, verify
from infra.spec import ROOT, alarm_descriptors, load
from scripts.dev.validate_durable import examples
from scripts.dev.validate_observations import fixtures
from tracaus import agentcore, observability, pipeline
from tracaus.ledger import Ledger

SPEC = load(ROOT / "examples/observability.example.json")
CONFIG = json.loads((ROOT / "examples/durable.example.json").read_text())
IID = "a" * 32
REGION = SPEC["monitor_region"]
ACCOUNT = SPEC["account_id"]


@pytest.fixture
def env(monkeypatch):
    for k, v in {
        "MONITOR_REGION": REGION,
        "EXPECTED_ACCOUNT_ID": ACCOUNT,
        "INCIDENT_TABLE": "synthetic",
        "STATUS_BASE_URL": "https://status.example.invalid/",
        "REPORTS_TOPIC_ARN": f"arn:aws:sns:{REGION}:{ACCOUNT}:tracaus-staging-reports",
        "PRIMARY_EMAIL": SPEC["notification_email"],
        "FALLBACK_TOPIC_ARN": f"arn:aws:sns:{REGION}:{ACCOUNT}:tracaus-staging-observation-fallback",
        "FALLBACK_EMAIL": "fallback@example.invalid",
        "HEALTH_NAMESPACE": "tracaus/staging/Health",
        "OBS_SETTINGS": json.dumps({**SPEC["observability"], "enabled": True}),
        "INITIAL_QUEUE_URL": "initial",
        "REPORT_QUEUE_URL": "report",
        "WORK_QUEUE_URL": "work",
    }.items():
        monkeypatch.setenv(k, v)


def role_fixture():
    planned = {
        "Path": "/tracaus/staging/",
        "AssumeRolePolicyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        },
        "Policies": [{"PolicyName": "runtime", "PolicyDocument": {"Version": "2012-10-17", "Statement": []}}],
    }
    client = MagicMock()
    client.get_role.return_value = {
        "Role": {
            "Path": planned["Path"],
            "AssumeRolePolicyDocument": copy.deepcopy(planned["AssumeRolePolicyDocument"]),
        }
    }
    client.list_role_policies.return_value = {"PolicyNames": ["runtime"]}
    client.list_attached_role_policies.return_value = {"AttachedPolicies": []}
    client.get_role_policy.return_value = {
        "PolicyDocument": copy.deepcopy(planned["Policies"][0]["PolicyDocument"])
    }
    return client, planned


@pytest.mark.parametrize("drift", ["trust", "grants", "attached", "inline", "boundary", "path"])
def test_role_drift_fails_closed(drift):
    client, planned = role_fixture()
    owned_ops.verify_role(client, "arn:aws:iam::123456789012:role/tracaus/staging/test", planned)
    if drift == "trust":
        client.get_role.return_value["Role"]["AssumeRolePolicyDocument"]["Statement"] = []
    elif drift == "grants":
        client.get_role_policy.return_value["PolicyDocument"]["Statement"] = [
            {"Action": "*", "Resource": "*", "Effect": "Allow"}
        ]
    elif drift == "attached":
        client.list_attached_role_policies.return_value["AttachedPolicies"] = [{"PolicyArn": "unexpected"}]
    elif drift == "inline":
        client.list_role_policies.return_value["PolicyNames"].append("extra")
    else:
        client.get_role.return_value["Role"]["PermissionsBoundary" if drift == "boundary" else "Path"] = (
            "unexpected"
        )
    with pytest.raises(verify.VerificationError):
        owned_ops.verify_role(client, "arn:aws:iam::123456789012:role/tracaus/staging/test", planned)


@pytest.mark.parametrize("logical", ["Ingress", "Dispatch", "Investigate", "Initial", "Report", "Reconcile"])
@pytest.mark.parametrize("drift", ["grant", "binding"])
def test_standalone_verification_checks_each_owned_role(tmp_path, logical, drift):
    stages, bindings, artifacts, versions = examples(SPEC, CONFIG, include_bindings=True)
    planned = stages["durable-runtime"]
    (tmp_path / "durable-runtime.json").write_text(json.dumps(planned))
    bundle = {
        "spec": SPEC,
        "directory": str(tmp_path),
        "stages": {"durable-runtime": {"template_hash": durable_ops.templates.template_hash(planned)}},
        "bindings": {**bindings, "artifacts": artifacts, "versions": versions},
    }
    cfn, lam = MagicMock(), MagicMock()
    cfn.get_stack_policy.return_value = {"StackPolicyBody": json.dumps(release.SEALED_POLICY)}
    cfn.get_template.return_value = {"TemplateBody": planned}
    cfn.describe_stack_resource.side_effect = lambda **k: {
        "StackResourceDetail": {"PhysicalResourceId": k["LogicalResourceId"]}
    }

    def configuration(**kwargs):
        key = next(
            k.removesuffix("VersionArn") for k, arn in versions.items() if arn == kwargs["FunctionName"]
        )
        return {
            **planned["Resources"][key]["Properties"],
            "Role": f"arn:aws:iam::{ACCOUNT}:role/tracaus/staging/{key}Role"
            if drift != "binding" or key != logical
            else f"arn:aws:iam::{ACCOUNT}:role/tracaus/staging/other",
        }

    lam.get_function_configuration.side_effect = configuration
    lam.get_function_concurrency.side_effect = lambda **k: (
        {"ReservedConcurrentExecutions": CONFIG["initial_reserved_concurrency"]}
        if "incident-initial" in k["FunctionName"]
        else {}
    )

    def grant_check(client, arn, properties):
        if drift == "grant" and arn.endswith("/" + logical + "Role"):
            raise verify.VerificationError("injected policy drift")

    with (
        patch.object(durable_ops, "assert_account"),
        patch.object(
            durable_ops,
            "owned_stack",
            return_value=(cfn, {"StackStatus": "CREATE_COMPLETE", "EnableTerminationProtection": True}),
        ),
        patch.object(durable_ops, "collect", return_value=versions),
        patch.object(
            durable_ops,
            "verify_function",
            side_effect=lambda client, arn, artifact: {
                "configuration": configuration(FunctionName=arn)["Environment"]["Variables"]
            },
        ),
        patch.object(owned_ops, "verify_role", side_effect=grant_check),
    ):
        with pytest.raises(
            verify.VerificationError, match="injected policy drift" if drift == "grant" else "role binding"
        ):
            durable_ops.verify_runtime(
                bundle, lambda service, region: lam if service == "lambda" else MagicMock()
            )


def coverage_factory(spec, health):
    clients = {k: MagicMock() for k in ("sts", "lambda", "ec2", "cloudwatch", "logs")}
    clients["sts"].get_caller_identity.return_value = {"Account": ACCOUNT}
    clients["lambda"].get_account_settings.return_value = {
        "AccountLimit": {"UnreservedConcurrentExecutions": 1000}
    }
    clients["ec2"].describe_instances.return_value = {
        "Reservations": [
            {"Instances": [{"InstanceId": i["id"], "State": {"Name": "running"}} for i in spec["instances"]]}
        ]
    }

    def metrics(**kw):
        rows = [
            d
            for d in alarm_descriptors(spec)
            if d["namespace"] == kw["Namespace"]
            and d["metric_name"] == kw["MetricName"]
            and (health or not d["namespace"].endswith("/Health"))
        ]
        return {
            "Metrics": [
                {"Dimensions": [{"Name": k, "Value": v} for k, v in d["dimensions"].items()]} for d in rows
            ]
        }

    clients["cloudwatch"].list_metrics.side_effect = metrics
    clients["logs"].test_metric_filter.return_value = {"matches": [{"eventNumber": 1}]}
    clients["logs"].describe_log_groups.side_effect = lambda **k: {
        "logGroups": [{"logGroupName": k["logGroupNamePrefix"]}]
    }
    return lambda service, region: clients[service]


def test_paused_health_is_explicit_and_enabled_bootstrap_stays_strict():
    spec = copy.deepcopy(SPEC)
    spec["reference_only"] = False
    paused = verify.coverage(spec, coverage_factory(spec, health=False))
    assert paused["observation_mode"] == "disabled"
    assert any(c["status"] == "disabled_by_reviewed_inventory" for c in paused["metrics"])
    spec["observability"]["enabled"] = True
    with pytest.raises(verify.VerificationError, match="Required metric unavailable"):
        verify.coverage(spec, coverage_factory(spec, health=False))
    assert verify.coverage(spec, coverage_factory(spec, health=True))["status"] == "PASS"


def test_seed_health_verifies_runtime_and_only_invokes_reviewed_services():
    spec = copy.deepcopy(SPEC)
    spec["reference_only"] = False
    spec["observability"]["enabled"] = True
    spec["observability"]["services"].append({**spec["observability"]["services"][0], "id": "second"})
    client = MagicMock()
    client.invoke.side_effect = lambda **k: {
        "StatusCode": 200,
        "Payload": BytesIO(b'{"status":"CHECKED","mode":"health"}'),
    }
    bundle = {"spec": spec, "bindings": {"observation_versions": {"ObserverVersionArn": "qualified:1"}}}
    with (
        patch.object(durable_ops, "require_reviewed_source"),
        patch.object(observations, "verify_runtime") as checked,
    ):
        assert observations.seed_health(bundle, lambda *args: client)["services"] == [
            s["id"] for s in spec["observability"]["services"]
        ]
    checked.assert_called_once()
    assert [json.loads(c.kwargs["Payload"])["service_id"] for c in client.invoke.call_args_list] == [
        s["id"] for s in spec["observability"]["services"]
    ]
    client.invoke.assert_called_with(
        FunctionName="qualified:1",
        InvocationType="RequestResponse",
        Payload=json.dumps({"mode": "health", "service_id": "second"}).encode(),
    )


def test_seed_reference_denied_before_source_or_clients():
    factory = MagicMock()
    with pytest.raises(verify.VerificationError, match="Synthetic"):
        observations.seed_health({"spec": SPEC}, factory)
    factory.assert_not_called()


@pytest.mark.parametrize("function_error", [False, True])
def test_failed_health_bootstrap_does_not_produce_a_seed_receipt(function_error):
    spec = copy.deepcopy(SPEC)
    spec["reference_only"] = False
    spec["observability"]["enabled"] = True
    client, body = MagicMock(), BytesIO(b'{"status":"ATTENTION","mode":"health"}')
    client.invoke.return_value = {
        "StatusCode": 200,
        "Payload": body,
        **({"FunctionError": "Unhandled"} if function_error else {}),
    }
    bundle = {"spec": spec, "bindings": {"observation_versions": {"ObserverVersionArn": "qualified:1"}}}
    with patch.object(durable_ops, "require_reviewed_source"), patch.object(observations, "verify_runtime"):
        with pytest.raises(verify.VerificationError, match="bootstrap incomplete"):
            observations.seed_health(bundle, lambda *args: client)
    assert body.closed


@pytest.mark.parametrize("apply", [False, True])
def test_notification_replay_reference_denied_before_aws(tmp_path, monkeypatch, apply):
    from scripts import replay_notification

    argv = [
        "replay_notification",
        "--spec",
        str(ROOT / "examples/observability.example.json"),
        "--incident-id",
        IID,
        "--kind",
        "INITIAL",
        "--output",
        str(tmp_path / "result.json"),
    ]
    if apply:
        argv.extend(["--apply", str(tmp_path / "unread-review.json")])
    monkeypatch.setattr(sys, "argv", argv)
    with patch.object(replay_notification.boto3, "client") as client:
        assert replay_notification.main() == 1
    client.assert_not_called()
    assert not (tmp_path / "result.json").exists()


def test_persisted_scan_progress_escapes_poison_prefix_across_invocations(env, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: clock[0])
    rows = [Ledger.intent(f"{i:032x}", "INITIAL", "2026-01-01T00:00:00Z", 4102444800) for i in range(6)]
    state = {"revision": 0, "cursor": None}
    store, seen = MagicMock(), []
    store.sweep_state.side_effect = lambda kind: copy.deepcopy(state)

    def save(kind, revision, cursor):
        assert revision == state["revision"]
        state.update(revision=revision + 1, cursor=cursor)
        return "SAVED"

    store.save_sweep.side_effect = save

    def query(value, cursor):
        start = next((i + 1 for i, r in enumerate(rows) if cursor and r["PK"] == cursor["PK"]), 0)
        return rows[start:], None

    store.pending.side_effect = query

    def send(store, row, sqs):
        assert state["cursor"]["PK"] == row["PK"]  # checkpoint survives a killed operation
        seen.append(row["PK"])
        clock[0] += 13
        if row != rows[-1]:
            raise RuntimeError("poison row")

    with (
        patch.object(pipeline, "ledger", return_value=store),
        patch.object(pipeline, "clients"),
        patch.object(pipeline, "dispatch_row", side_effect=send),
    ):
        for _ in range(3):
            with pytest.raises(RuntimeError, match="Reconciliation incomplete"):
                pipeline.reconcile({"sweep": "pending"})
    assert seen == [r["PK"] for r in rows]
    assert state["cursor"] is None  # wrap permits failed rows to retry later
    store.expired.assert_not_called()
    store.overdue.assert_not_called()


def test_competing_sweep_cannot_overwrite_newer_cursor(env):
    store = MagicMock()
    row = Ledger.intent(IID, "INITIAL", "2026-01-01T00:00:00Z", 4102444800)
    store.sweep_state.return_value = {"revision": 3, "cursor": None}
    store.pending.return_value = ([row], None)
    store.save_sweep.return_value = "STALE"
    with patch.object(pipeline, "ledger", return_value=store), patch.object(pipeline, "clients"):
        assert pipeline.reconcile({"sweep": "pending"})["checked"] == 0
    store.dispatch.assert_not_called()
    assert store.save_sweep.call_count == 1


def test_notification_recovery_is_independent_of_failing_worker_scan(env):
    store = MagicMock()
    row = {
        "PK": "INCIDENT#" + IID,
        "SK": "NOTIFICATION#INITIAL",
        "GSI2PK": "NOTIFY",
        "GSI2SK": "000000000000#" + IID,
    }
    store.sweep_state.return_value = {"revision": 0, "cursor": None}
    store.save_sweep.return_value = "SAVED"
    store.expired_notifications.return_value = ([row], None)
    store.get.return_value = row
    store.expired.side_effect = RuntimeError("unavailable")
    with patch.object(pipeline, "ledger", return_value=store), patch.object(pipeline, "clients"):
        assert pipeline.reconcile({"sweep": "notifications"})["checked"] == 1
    store.recover_notification.assert_called_once()
    store.expired.assert_not_called()


@pytest.mark.parametrize("target", ["standalone", "agentcore"])
def test_all_four_recovery_classes_have_independent_scheduled_invocations(target):
    stages = examples(SPEC, {**CONFIG, "runtime_target": target})
    targets = stages["routing"]["Resources"]["SweepRule"]["Properties"]["Targets"]
    assert {json.loads(t["Input"])["sweep"] for t in targets} == {
        "pending",
        "overdue",
        "expired",
        "notifications",
    }
    assert len({t["Id"] for t in targets}) == 4


def result_payload():
    return {
        "version": 1,
        "release": "test",
        "complete": True,
        "text": "safe",
        "code": "DONE",
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "tools": [],
        "evidence": [],
    }


def test_fragmented_sse_result_and_oversized_newline_free_body_are_bounded():
    result = result_payload()

    class Fragments(BytesIO):
        def read(self, n=-1):
            return super().read(min(n, 7))

    stream = Fragments(
        b": heartbeat\r\n\r\ndata: "
        + json.dumps({"event": "result", "result": result}).encode()
        + b"\r\n\r\n"
    )
    client = MagicMock()
    client.invoke_agent_runtime.return_value = {
        "statusCode": 200,
        "contentType": "text/event-stream",
        "response": stream,
    }
    args = dict(
        arn="synthetic",
        qualifier="release",
        region=REGION,
        account=ACCOUNT,
        session_id="synthetic",
        deadline=time.time() + 30,
        client=client,
    )
    assert agentcore.invoke_stream({"release": "test"}, **args) == result
    assert stream.closed
    large = BytesIO(b"x" * (agentcore.MAX_WIRE_BYTES * 2))
    with pytest.raises(RuntimeError, match="limit"):
        list(agentcore.bounded_lines(large, time.time() + 30))
    assert large.tell() <= agentcore.MAX_WIRE_BYTES + 1024


@pytest.mark.parametrize(
    "behavior", ["silent", "trickle", "disconnect", "oversized", "result-open", "production-result"]
)
def test_real_sdk_socket_has_absolute_deadline_and_child_is_reaped(behavior, monkeypatch):
    received = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", "999999")
            self.end_headers()
            try:
                if behavior == "oversized":
                    self.wfile.write(b"x" * (agentcore.MAX_WIRE_BYTES + 1))
                    self.wfile.flush()
                elif behavior in {"result-open", "production-result"}:
                    self.wfile.write(
                        b"data: "
                        + json.dumps({"event": "result", "result": result_payload()}).encode()
                        + b"\n\n"
                    )
                    self.wfile.flush()
                    time.sleep(3)
                elif behavior == "disconnect":
                    self.wfile.write(b'data: {"event":')
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)
                else:
                    until = time.monotonic() + 3
                    while time.monotonic() < until:
                        if behavior == "trickle":
                            self.wfile.write(b"x")
                            self.wfile.flush()
                        time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # The only SDK network path is in this child to an explicit loopback URL,
    # using dummy credentials and no retries. Parent tests continue denying AWS.
    code = """import boto3,json,sys,time
from botocore.config import Config
from tracaus.agentcore import invoke_stream
c=boto3.client('bedrock-agentcore',region_name='eu-central-1',endpoint_url=sys.argv[1],aws_access_key_id='synthetic',aws_secret_access_key='synthetic',config=Config(connect_timeout=1,read_timeout=10,retries={'total_max_attempts':1}))
print(json.dumps(invoke_stream({'release':'test'},arn='arn:aws:bedrock-agentcore:eu-central-1:123456789012:runtime/test-1234567890',qualifier='release_test',region='eu-central-1',account='123456789012',session_id='incident_'+'a'*40,deadline=time.time()+30,client=c)))
"""
    children = []
    original = subprocess.Popen

    def launch(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    start = time.time()
    try:
        with patch.object(agentcore.subprocess, "Popen", side_effect=launch):
            if behavior == "production-result":
                monkeypatch.setenv("AWS_ENDPOINT_URL", f"http://127.0.0.1:{server.server_port}")
                monkeypatch.setenv(
                    "AWS_ENDPOINT_URL_BEDROCK_AGENTCORE", f"http://127.0.0.1:{server.server_port}"
                )
                monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic")
                monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic")
                monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
                monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
                monkeypatch.delenv("AWS_PROFILE", raising=False)
                monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
                assert (
                    agentcore.invoke(
                        {"release": "test"},
                        arn=f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/test-1234567890",
                        qualifier="release_test",
                        region=REGION,
                        account=ACCOUNT,
                        session_id="incident_" + IID + "a",
                        deadline=start + 30,
                    )
                    == result_payload()
                )
            elif behavior == "result-open":
                output = agentcore.run_child(
                    [sys.executable, "-c", code, f"http://127.0.0.1:{server.server_port}"], b"", start + 2
                )
                assert json.loads(output) == result_payload()
            else:
                with pytest.raises(RuntimeError):
                    agentcore.run_child(
                        [sys.executable, "-c", code, f"http://127.0.0.1:{server.server_port}"], b"", start + 2
                    )
        assert received.is_set(), "Test must reach the real SDK socket before interruption"
        assert time.time() - start < 3
        assert len(children) == 1 and children[0].poll() is not None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize("attempts,outcome", [(1, "RETRY"), (2, "RETRY"), (3, "AMBIGUOUS")])
def test_expired_notification_recovers_atomically_without_paid_work(attempts, outcome):
    client = MagicMock()
    store = Ledger("synthetic", client, MagicMock())
    row = {
        "PK": "INCIDENT#" + IID,
        "SK": "NOTIFICATION#INITIAL",
        "status": "SENDING",
        "attempts": attempts,
        "lease_owner": "dead",
        "lease_until": 10,
        "fencing_token": 3,
        "ttl": 4102444800,
    }
    assert store.recover_notification(row, 11) == outcome
    writes = client.transact_write_items.call_args.kwargs["TransactItems"]
    assert writes[0]["Update"]["ExpressionAttributeValues"][":next"]["S"] == (
        "AMBIGUOUS" if attempts == 3 else "PENDING"
    )
    assert "fencing_token" in writes[0]["Update"]["ConditionExpression"]
    puts = [w["Put"]["Item"]["SK"]["S"] for w in writes if "Put" in w]
    assert puts == [] if attempts == 3 else len(puts) == 1 and puts[0].startswith("INTENT#INITIAL#")
    assert not any("WORK" in sk for sk in puts)


def test_stale_notification_recovery_and_cursor_cas_do_not_succeed():
    failure = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
    client, table = MagicMock(), MagicMock()
    store = Ledger("synthetic", client, table)
    table.update_item.side_effect = failure
    assert store.save_sweep("pending", 0, None) == "STALE"
    client.transact_write_items.side_effect = failure
    row = {
        "PK": "INCIDENT#" + IID,
        "SK": "NOTIFICATION#INITIAL",
        "status": "SENDING",
        "attempts": 3,
        "lease_owner": "dead",
        "lease_until": 10,
        "fencing_token": 3,
        "ttl": 4102444800,
    }
    assert store.recover_notification(row, 11) == "STALE"


def test_reviewed_notification_replay_only_writes_notification_intent_and_audit():
    client, table = MagicMock(), MagicMock()
    store = Ledger("synthetic", client, table)
    notification = {"status": "AMBIGUOUS", "attempts": 3, "fencing_token": 4, "ttl": 4102444800}
    table.get_item.side_effect = lambda **k: {
        "Item": notification
        if k["Key"]["SK"].startswith("NOTIFICATION")
        else {"status": "COMPLETE", "ttl": 4102444800, "model_calls": 5}
    }
    plan = store.notification_replay_plan(IID, "INITIAL")
    assert store.replay_notification(plan, "synthetic-operator")["status"] == "NOTIFICATION_REPLAY_RECORDED"
    writes = client.transact_write_items.call_args.kwargs["TransactItems"]
    assert writes[0]["ConditionCheck"]["Key"]["SK"]["S"] == "META"
    assert writes[1]["Update"]["Key"]["SK"]["S"] == "NOTIFICATION#INITIAL"
    assert writes[2]["Put"]["Item"]["SK"]["S"].startswith("INTENT#INITIAL#")
    assert writes[3]["Put"]["Item"]["record_type"]["S"] == "notification_replay"
    assert "model_calls" not in str(writes) and "INTENT#WORK" not in str(writes)
    notification["fencing_token"] += 1
    with pytest.raises(ValueError, match="changed"):
        store.replay_notification(plan, "synthetic-operator")
    assert client.transact_write_items.call_count == 1


@pytest.mark.parametrize("changes", [{"status": "PUBLISHER_ACCEPTED"}, {"ttl": 1}, {"replay_runs": 2}])
def test_notification_replay_refuses_success_expiry_or_exhausted_allowance(changes):
    client, table = MagicMock(), MagicMock()
    row = {"status": "AMBIGUOUS", "attempts": 3, "fencing_token": 3, "ttl": 4102444800, **changes}
    table.get_item.return_value = {"Item": row}
    with pytest.raises(ValueError):
        Ledger("synthetic", client, table).notification_replay_plan(IID, "REPORT")
    client.transact_write_items.assert_not_called()


def test_notification_batch_retains_second_record_without_claim_when_time_is_short(env):
    store, sns, context = MagicMock(), MagicMock(), MagicMock()
    context.get_remaining_time_in_millis.side_effect = [60000, 20000]
    store.get.side_effect = [
        {"status": "PENDING", "attempts": 0},
        {"instance_id": "synthetic", "occurred_at": "2026-10-06T00:00:00Z", "status": "PENDING"},
    ]
    store.claim_notification.return_value = {"attempts": 1, "fencing_token": 1}
    sns.publish.return_value = {"MessageId": "first"}
    records = [{"messageId": str(i), "body": "INCIDENT#" + IID + "|INTENT#INITIAL#1"} for i in range(2)]
    with (
        patch.object(pipeline, "ledger", return_value=store),
        patch.object(pipeline, "clients", return_value=sns),
    ):
        assert pipeline.initial({"Records": records}, context) == {
            "batchItemFailures": [{"itemIdentifier": "1"}]
        }
    assert store.claim_notification.call_count == 1 and sns.publish.call_count == 1


def test_first_delivered_receipt_survives_lost_ack_and_later_accepted_retry(env):
    now, slot = 172800 + 1000, 172800
    ledger = MagicMock()
    row = {
        "incident_id": IID,
        "due_epoch": slot + 600,
        "recipient_received_at": slot + 50,
        "recipient_message_id": "first-ack-lost",
        "recipient_message_ids": {"first-ack-lost"},
        "recipient_notification_id": IID + "-initial",
    }
    notification = {
        "status": "PUBLISHER_ACCEPTED",
        "publisher_message_id": "second-accepted",
        "publisher_message_ids": {"second-accepted"},
    }

    def get(pk, sk="META"):
        if pk == "CANARY#" + str(slot):
            return row
        if pk.startswith("INCIDENT#"):
            return notification if sk != "META" else {"status": "CANARY"}
        return None

    ledger.get.side_effect = get
    problems, received, age = observability.verify_canary(ledger, SPEC["observability"], now)
    assert received and "CANARY_RECEIPT_MISSED" not in problems
    assert "EMAIL_RECEIPT_UNVERIFIED" in problems  # SQS does not attest a mailbox
    notification["status"] = "SENDING"
    assert not observability.verify_canary(ledger, SPEC["observability"], now)[1]


def test_receipts_preserve_first_time_and_collect_reordered_duplicate_ids(env):
    ledger = MagicMock()
    row = {"incident_id": IID, "ttl": 4102444800}
    ledger.get.return_value = {"canary_slot": 172800, "ttl": 4102444800}

    def update(**kwargs):
        values = kwargs["ExpressionAttributeValues"]
        assert "if_not_exists(recipient_received_at,:now)" in kwargs["UpdateExpression"]
        assert "contains(recipient_message_ids,:message)" in kwargs["ConditionExpression"]
        assert values[":max"] == 9
        row.setdefault("recipient_received_at", values[":now"])
        row.setdefault("recipient_message_id", values[":message"])
        row["recipient_notification_id"] = values[":notification"]
        ids = row.setdefault("recipient_message_ids", set())
        if len(ids) >= 9 and values[":message"] not in ids:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        ids.update(values[":messages"])

    ledger.table.update_item.side_effect = update

    def envelope(message_id):
        return {
            "messageId": message_id,
            "body": json.dumps(
                {
                    "Type": "Notification",
                    "TopicArn": f"arn:aws:sns:{REGION}:{ACCOUNT}:tracaus-staging-reports",
                    "MessageId": message_id,
                    "MessageAttributes": {
                        "tracaus_incident": {"Value": IID},
                        "tracaus_canary": {"Value": "true"},
                        "tracaus_notification": {"Value": IID + "-initial"},
                    },
                }
            ),
        }

    with patch.object(observability, "store", return_value=ledger):
        assert (
            observability.recipient({"Records": [envelope("second"), envelope("first"), envelope("second")]})[
                "batchItemFailures"
            ]
            == []
        )
        assert row["recipient_message_id"] == "second" and row["recipient_message_ids"] == {"first", "second"}
        first_time = row["recipient_received_at"]
        assert observability.recipient({"Records": [envelope(str(i)) for i in range(8)]}) == {
            "batchItemFailures": [{"itemIdentifier": "7"}]
        }
        assert len(row["recipient_message_ids"]) == 9 and row["recipient_received_at"] == first_time


@pytest.mark.parametrize("stable", [None, "other-initial", IID + "-report"])
def test_receipt_rejects_wrong_notification_identity_before_writing(env, stable):
    ledger = MagicMock()
    message = {
        "Type": "Notification",
        "TopicArn": f"arn:aws:sns:{REGION}:{ACCOUNT}:tracaus-staging-reports",
        "MessageId": "synthetic",
        "MessageAttributes": {
            "tracaus_incident": {"Value": IID},
            "tracaus_canary": {"Value": "true"},
            "tracaus_notification": {"Value": stable},
        },
    }
    with patch.object(observability, "store", return_value=ledger):
        assert observability.recipient({"Records": [{"messageId": "q", "body": json.dumps(message)}]}) == {
            "batchItemFailures": [{"itemIdentifier": "q"}]
        }
    ledger.table.update_item.assert_not_called()


def test_multiple_slow_services_have_independent_budgets_and_publish_before_freshness(env, monkeypatch):
    config = {**SPEC["observability"], "enabled": True}
    config["services"] = [{**config["services"][0], "id": f"service{i}"} for i in range(3)]
    monkeypatch.setenv("OBS_SETTINGS", json.dumps(config))
    clock = [0.0]
    monkeypatch.setattr(observability.time, "monotonic", lambda: clock[0])
    cw, client, ledger, context = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    context.get_remaining_time_in_millis.return_value = 180000

    def probe(route):
        clock[0] += 6
        return {"healthy": True}

    def fresh(service, *args):
        # Availability must already be published even if freshness later fails.
        assert cw.put_metric_data.call_args.kwargs["MetricData"][0]["MetricName"] == "Availability"
        clock[0] += 35
        raise RuntimeError("collector read unavailable")

    with (
        patch.object(observability, "clients", side_effect=lambda s: cw if s == "cloudwatch" else client),
        patch.object(observability, "store", return_value=ledger),
        patch.object(observability.probes, "bounded_check", side_effect=probe),
        patch.object(observability, "check_freshness", side_effect=fresh),
    ):
        for service in config["services"]:
            response = observability.observer({"mode": "health", "service_id": service["id"]}, context)
            assert response["status"] == "ATTENTION" and "CHECKS_INCOMPLETE" not in response["failures"]
    assert cw.put_metric_data.call_count == 6
    assert {
        c.kwargs["MetricData"][0]["Dimensions"][0]["Value"] for c in cw.put_metric_data.call_args_list
    } == {s["id"] for s in config["services"]}
    assert all(
        c.kwargs["MetricData"][0]["Value"] == 0
        for c in cw.put_metric_data.call_args_list
        if c.kwargs["MetricData"][0]["MetricName"] == "TelemetryFresh"
    )
    ledger.pending.assert_not_called()


def test_delivery_deadline_is_explicit_before_unguarded_tail_reads(env):
    client, ledger, context = MagicMock(), MagicMock(), MagicMock()
    context.get_remaining_time_in_millis.return_value = 20000
    with (
        patch.object(observability, "clients", return_value=client),
        patch.object(observability, "store", return_value=ledger),
    ):
        result = observability.observer({"mode": "delivery"}, context)
    assert result["status"] == "ATTENTION" and "CHECKS_INCOMPLETE" in result["failures"]
    ledger.get.assert_not_called()
    client.get_paginator.assert_not_called()


def test_recipient_discovery_refuses_unbounded_subscription_paging(env):
    client = MagicMock()
    client.get_paginator.return_value.paginate.return_value = iter(
        [{"Subscriptions": [], "NextToken": "more"}] * 3
    )
    with pytest.raises(RuntimeError, match="page limit"):
        observability.confirmed_recipient(client, deadline=time.monotonic() + 100)
    client.get_subscription_attributes.assert_not_called()


@pytest.mark.parametrize("target", ["standalone", "agentcore"])
def test_health_schedules_and_dead_letter_policies_match_each_service(target):
    rendered = fixtures(SPEC, {**CONFIG, "runtime_target": target})
    active = rendered["observations"]["Resources"]
    schedules = [r["Properties"] for r in active.values() if r["Type"] == "AWS::Events::Rule"]
    health = [r for r in schedules if r["Name"].find("obs-health-") != -1]
    assert len(health) == len(SPEC["observability"]["services"])
    policy = rendered["observation-foundation"]["Resources"]["DeadPolicy"]["Properties"]["PolicyDocument"][
        "Statement"
    ][-1]
    allowed = policy["Condition"]["ArnEquals"]["aws:SourceArn"]
    for r in health:
        assert f"arn:aws:events:{REGION}:{ACCOUNT}:rule/{r['Name']}" in allowed
        assert json.loads(r["Targets"][0]["Input"])["mode"] == "health"
    delivery = active["ObserverSchedule"]["Properties"]["Targets"][0]
    assert json.loads(delivery["Input"]) == {"mode": "delivery"}


@pytest.mark.parametrize("region", [REGION, "us-east-1"])
def test_agentcore_dashboard_and_retained_logs_use_remote_endpoint_region(region):
    spec = {**SPEC, "bedrock_region": region}
    stages = fixtures(spec, {**CONFIG, "runtime_target": "agentcore"})
    widgets = json.loads(stages["observations"]["Resources"]["Dashboard"]["Properties"]["DashboardBody"])[
        "widgets"
    ]
    for widget in widgets:
        p = widget["properties"]
        if p["title"] in {
            "model",
            "tool",
            "AgentCore application handoffs",
            "AgentCore release endpoint health",
        }:
            assert p["region"] == region
    native = next(
        w["properties"] for w in widgets if w["properties"]["title"] == "AgentCore release endpoint health"
    )
    assert all(
        "Operation" in m and "InvokeAgentRuntime" in m and "Resource" in m and "DEFAULT" not in str(m)
        for m in native["metrics"]
    )
    endpoint = stages["agentcore-endpoint"]["Resources"]
    assert set(endpoint["Endpoint"]["DependsOn"]) == {"RuntimeLogs", "DefaultLogs"}
    for logical in ("RuntimeLogs", "DefaultLogs"):
        assert endpoint[logical]["DeletionPolicy"] == "Retain"
        assert endpoint[logical]["Properties"]["RetentionInDays"] == spec["log_retention_days"]
    query = next(
        w["properties"]["query"]
        for w in widgets
        if w["properties"]["title"] == "AgentCore application handoffs"
    )
    assert endpoint["RuntimeLogs"]["Properties"]["LogGroupName"] in query


@pytest.mark.parametrize("drift", ["absent", "retention", "pagination"])
def test_actual_agentcore_log_retention_drift_is_denied(drift):
    stages, bindings, artifacts, versions = examples(
        SPEC, {**CONFIG, "runtime_target": "agentcore"}, include_bindings=True
    )
    client = MagicMock()

    def groups(**k):
        response = {
            "logGroups": [
                {"logGroupName": k["logGroupNamePrefix"], "retentionInDays": SPEC["log_retention_days"]}
            ]
        }
        if drift == "absent":
            response["logGroups"] = []
        elif drift == "retention":
            response["logGroups"][0]["retentionInDays"] = 3653
        else:
            response["nextToken"] = "ambiguous"
        return response

    client.describe_log_groups.side_effect = groups
    with pytest.raises(verify.VerificationError, match="log group or retention"):
        owned_ops.verify_agentcore_logs(SPEC, bindings["agentcore"], lambda *args: client)
