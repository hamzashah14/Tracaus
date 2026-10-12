"""Runtime contract and side-effect denial tests; no customer credentials/network."""

import json
import threading
import time
from dataclasses import asdict
from io import BytesIO
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from tracaus import agentcore, agentcore_host, execution, runtime
from tracaus.ledger import Ledger

IID = "i-0123456789abcdef0"
ACCOUNT = "123456789012"
REGION = "eu-central-1"
ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/tracaus_example-1234567890"
RELEASE = "a" * 64


def answer(text="Evidence is inconclusive.", stop="end_turn", blocks=None):
    return {
        "usage": {"inputTokens": 100, "outputTokens": 20},
        "stopReason": stop,
        "output": {"message": {"role": "assistant", "content": blocks or [{"text": text}]}},
    }


def drive(client=None, policy=None, budget=None, **kwargs):
    policy = policy or runtime.Limits()
    budget = budget or runtime.MemoryBudget(policy)
    client = client or MagicMock()
    client.count_tokens.return_value = {"inputTokens": 100}
    return (
        runtime.run(
            "Investigate",
            model_id="model-v1",
            region=REGION,
            tools=kwargs.pop("tools", MagicMock()),
            reserve=budget.reserve,
            limits=policy,
            deadline=kwargs.pop("deadline", time.time() + 180),
            client=client,
            **kwargs,
        ),
        client,
        budget,
    )


def test_exact_counted_input_and_conservative_reservation_precede_inference():
    client = MagicMock()
    client.converse.return_value = answer()
    calls = []
    client.count_tokens.side_effect = lambda **kw: calls.append("count") or {"inputTokens": 100}
    budget = runtime.MemoryBudget(runtime.Limits())
    original = budget.reserve
    budget.reserve = lambda delta: calls.append("reserve") or original(delta)
    client.converse.side_effect = lambda **kw: calls.append("infer") or answer()
    result, _, budget = drive(client, budget=budget)
    assert result["complete"] and calls == ["count", "reserve", "infer"]
    request = client.converse.call_args.kwargs
    counted = client.count_tokens.call_args.kwargs["input"]["converse"]
    assert counted == {k: request[k] for k in ("messages", "system", "toolConfig")}
    assert budget.used["tokens_reserved"] == 1124  # Unused output is intentionally not refunded.


def test_exhausted_aggregate_allowance_prevents_inference_on_later_attempt():
    policy = runtime.Limits(tokens_reserved=1500)
    budget = runtime.MemoryBudget(policy)
    client = MagicMock()
    client.converse.return_value = answer()
    assert drive(client, policy, budget)[0]["complete"]
    result = drive(client, policy, budget)[0]
    assert result["code"] == "BUDGET_EXHAUSTED" and client.converse.call_count == 1


def test_unsupported_count_tokens_fails_closed_without_inference():
    client = MagicMock()
    client.count_tokens.side_effect = ClientError({"Error": {"Code": "ValidationException"}}, "CountTokens")
    with pytest.raises(ClientError):
        drive(client)
    client.converse.assert_not_called()


@pytest.mark.parametrize(
    "usage",
    [
        {"inputTokens": 101, "outputTokens": 20},
        {"inputTokens": 100, "outputTokens": 1025},
        {"inputTokens": 100, "outputTokens": 20, "cacheReadInputTokens": 1},
        {"inputTokens": True, "outputTokens": 20},
        {},
    ],
)
def test_accounting_mismatch_stops_before_tools(usage):
    client = MagicMock()
    client.converse.return_value = {**answer(), "usage": usage}
    tools = MagicMock()
    assert drive(client, tools=tools)[0]["code"] == "TOKEN_ACCOUNTING_MISMATCH"
    tools.invoke.assert_not_called()


def test_incident_requires_both_tool_contracts_and_final_turn():
    client, tools = MagicMock(), MagicMock()
    client.converse.side_effect = [
        answer(
            stop="tool_use",
            blocks=[
                {"toolUse": {"toolUseId": "logs", "name": "fetch_logs", "input": {"instance_id": IID}}},
                {"toolUse": {"toolUseId": "metrics", "name": "fetch_metrics", "input": {"instance_id": IID}}},
            ],
        ),
        answer(),
    ]
    tools.invoke.return_value = ({"status": "ok", "complete": True, "log_group": "/authorized/group"}, True)
    result, _, _ = drive(client, tools=tools, require_evidence=True)
    assert result["complete"] and tools.invoke.call_count == 2
    client.converse.side_effect = None
    client.converse.return_value = answer()
    assert drive(client, require_evidence=True)[0]["complete"] is False


def tool_client():
    client = MagicMock()
    client.invoke.return_value = {
        "StatusCode": 200,
        "Payload": BytesIO(
            json.dumps(
                {
                    "response": {
                        "httpStatusCode": 200,
                        "responseBody": {
                            "application/json": {
                                "body": json.dumps(
                                    {
                                        "status": "no_log_groups_found",
                                        "instance_id": IID,
                                        "complete": True,
                                        "truncated": False,
                                    }
                                )
                            }
                        },
                    }
                }
            ).encode()
        ),
    }
    return client


def tool_adapter(client=None, policy=None, budget=None):
    policy = policy or runtime.Limits()
    budget = budget or runtime.MemoryBudget(policy)
    return runtime.LambdaTools(
        REGION,
        ACCOUNT,
        {
            name: f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{name}:3"
            for name in ("fetch_logs", "fetch_metrics")
        },
        {IID},
        policy,
        budget.reserve,
        "2026-10-05T10:00:00Z",
        client or tool_client(),
    ), budget


@pytest.mark.parametrize(
    "name,args",
    [
        ("shell", {"instance_id": IID}),
        ("fetch_logs", {"instance_id": "i-11111111111111111"}),
        ("fetch_logs", {"instance_id": IID, "hours_back": "1"}),
        ("fetch_logs", {"instance_id": IID, "window_minutes": "180"}),
        ("fetch_logs", {"instance_id": IID, "time_string": "2026-10-04T10:00:00Z"}),
        ("fetch_logs", {"instance_id": IID, "unknown": "value"}),
        ("fetch_logs", {"instance_id": IID, "lines": "500"}),
    ],
)
def test_invalid_tool_authorization_or_window_has_no_side_effect(name, args):
    client = tool_client()
    tool, budget = tool_adapter(client)
    with pytest.raises(runtime.RuntimeStop):
        tool.invoke(name, args, time.time() + 180)
    client.invoke.assert_not_called()
    assert sum(budget.used.values()) == 0


def test_tool_response_contract_and_absolute_deadline_and_stream_close():
    client = tool_client()
    stream = client.invoke.return_value["Payload"]
    tool, budget = tool_adapter(client)
    result, complete = tool.invoke("fetch_logs", {"instance_id": IID}, time.time() + 180)
    assert complete and result["status"] == "no_log_groups_found" and stream.closed
    payload = json.loads(client.invoke.call_args.kwargs["Payload"])
    assert 0 < payload["runtime_deadline_epoch"] - time.time() <= 30
    assert {p["name"]: p["value"] for p in payload["parameters"]}["window_minutes"] == "15"
    assert budget.used["tool_calls"] == budget.used["log_queries"] == 1


def test_tool_query_reservation_consumed_on_ambiguous_remote_failure():
    client = tool_client()
    client.invoke.side_effect = TimeoutError("lost ack")
    tool, budget = tool_adapter(client)
    with pytest.raises(TimeoutError):
        tool.invoke("fetch_logs", {"instance_id": IID, "log_group_name": "/scoped/group"}, time.time() + 180)
    assert budget.used["tool_calls"] == 1 and budget.used["log_queries"] == 3


def test_ledger_reservation_enforces_fence_deadlines_policy_and_remaining_allowance():
    table = MagicMock()
    table.update_item.return_value = {"Attributes": dict.fromkeys(runtime.COUNTERS, 1)}
    store = Ledger("incidents", MagicMock(), table)
    claim = {"PK": "INCIDENT#" + "a" * 32, "lease_owner": "owner", "fencing_token": 2}
    store.begin_execution(claim, runtime.Limits().fingerprint)
    assert "execution_fence<>:token" in table.update_item.call_args.kwargs["ConditionExpression"]
    store.reserve(claim, runtime.Limits(), {"tokens_reserved": 1124, "model_steps": 1})
    kwargs = table.update_item.call_args.kwargs
    assert all(
        expr in kwargs["ConditionExpression"]
        for expr in (
            "fencing_token=:token",
            "execution_fence=:token",
            "lease_until>:now",
            "deadline_epoch>:now",
            "budget_policy_hash=:policy",
        )
    )
    assert kwargs["ExpressionAttributeValues"][":c0"] == 32000 - 1124
    table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
    )
    with pytest.raises(runtime.RuntimeStop):
        store.reserve(claim, runtime.Limits(), {"tool_calls": 1})


class Stream(BytesIO):
    def iter_lines(self, chunk_size):
        yield from self.getvalue().splitlines()


def test_agentcore_sse_release_binding_and_stream_closure():
    result = {
        "version": 1,
        "release": RELEASE,
        "text": "Report",
        "complete": True,
        "code": "",
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "tools": ["fetch_logs", "fetch_metrics"],
        "evidence": ["fetch_logs", "fetch_metrics"],
    }
    stream = Stream(
        b": heartbeat\n\n" + b"data: " + json.dumps({"event": "result", "result": result}).encode() + b"\n\n"
    )
    client = MagicMock()
    client.invoke_agent_runtime.return_value = {
        "response": stream,
        "statusCode": 200,
        "contentType": "text/event-stream",
    }
    assert (
        agentcore.invoke(
            {"release": RELEASE},
            arn=ARN,
            qualifier="release001",
            region=REGION,
            account=ACCOUNT,
            session_id="incident_" + "a" * 40,
            deadline=time.time() + 180,
            client=client,
        )
        == result
    )
    assert stream.closed and client.invoke_agent_runtime.call_count == 1


@pytest.mark.parametrize(
    "wire",
    [
        b": heartbeat\n\n",
        b'data: {"event":"error"}\n\n',
        b'data: {"event":"result","result":{"version":1,"release":"wrong","text":"report","complete":true}}\n\n',
    ],
)
def test_agentcore_ambiguous_or_mismatched_response_is_not_retried(wire):
    stream, client = Stream(wire), MagicMock()
    client.invoke_agent_runtime.return_value = {
        "response": stream,
        "statusCode": 200,
        "contentType": "text/event-stream",
    }
    with pytest.raises(RuntimeError):
        agentcore.invoke(
            {"release": RELEASE},
            arn=ARN,
            qualifier="release001",
            region=REGION,
            account=ACCOUNT,
            session_id="incident_" + "a" * 40,
            deadline=time.time() + 180,
            client=client,
        )
    assert stream.closed and client.invoke_agent_runtime.call_count == 1


def test_agentcore_host_sends_safe_failure_without_customer_exception():
    def denied(payload):
        raise ValueError("private customer log")

    output = b"".join(agentcore_host.events({}, denied))
    assert b"EXECUTION_FAILED" in output and b"private customer" not in output


def test_agentcore_host_recovers_capacity_when_execution_thread_cannot_start(monkeypatch):
    slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(agentcore_host, "SLOTS", slots)
    monkeypatch.setattr(agentcore_host, "ACTIVE", 0)
    thread = MagicMock()
    thread.start.side_effect = RuntimeError("thread unavailable")
    monkeypatch.setattr(agentcore_host.threading, "Thread", lambda **kwargs: thread)
    with pytest.raises(RuntimeError, match="thread unavailable"):
        next(agentcore_host.events({}))
    assert agentcore_host.ACTIVE == 0
    assert slots.acquire(blocking=False) and slots.acquire(blocking=False)
    assert not slots.acquire(blocking=False)


@pytest.mark.parametrize(
    "change",
    [
        {"version": True},
        {"usage": {"input_tokens": -1, "output_tokens": 0}},
        {"tools": ["unknown"]},
        {"evidence": ["fetch_logs", "fetch_logs"]},
    ],
)
def test_agentcore_rejects_invalid_remote_accounting_and_contract(change):
    result = {
        "version": 1,
        "release": RELEASE,
        "text": "Report",
        "complete": True,
        "code": "",
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "tools": [],
        "evidence": [],
        **change,
    }
    stream = Stream(b"data: " + json.dumps({"event": "result", "result": result}).encode() + b"\n\n")
    client = MagicMock()
    client.invoke_agent_runtime.return_value = {
        "response": stream,
        "statusCode": 200,
        "contentType": "text/event-stream",
    }
    with pytest.raises(RuntimeError, match="contract mismatch"):
        agentcore.invoke(
            {"release": RELEASE},
            arn=ARN,
            qualifier="release001",
            region=REGION,
            account=ACCOUNT,
            session_id="incident_" + "a" * 40,
            deadline=time.time() + 180,
            client=client,
        )
    assert stream.closed and client.invoke_agent_runtime.call_count == 1


def test_execution_rejects_stale_claim_before_model_tools_or_reservation(monkeypatch):
    monkeypatch.setenv("RUNTIME_RELEASE", RELEASE)
    monkeypatch.setenv("RUNTIME_LIMITS", json.dumps(asdict(runtime.Limits())))
    store = MagicMock()
    store.get.return_value = {"status": "RUNNING", "lease_owner": "different", "fencing_token": 1}
    payload = {
        "version": 1,
        "release": RELEASE,
        "mode": "incident",
        "incident_id": "a" * 32,
        "owner": "12345678-1234-1234-1234-123456789012",
        "fence": 1,
        "deadline": time.time() + 180,
    }
    with pytest.raises(runtime.RuntimeStop, match="STALE_EXECUTION"):
        execution.execute(payload, store=store)
    store.begin_execution.assert_not_called()
    store.reserve.assert_not_called()


def test_deadline_and_context_stop_before_inference():
    client = MagicMock()
    assert drive(client, deadline=time.time() + 1)[0]["code"] == "DEADLINE"
    assert drive(client, policy=runtime.Limits(context_bytes=1))[0]["code"] == "CONTEXT_LIMIT"
    client.count_tokens.assert_not_called()
    client.converse.assert_not_called()


def test_usage_is_persisted_before_any_tool_handoff():
    client = MagicMock()
    client.converse.return_value = answer(
        stop="tool_use",
        blocks=[{"toolUse": {"toolUseId": "log1", "name": "fetch_logs", "input": {"instance_id": IID}}}],
    )
    tools, observed = MagicMock(), MagicMock(side_effect=RuntimeError("ledger unavailable"))
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        drive(client, tools=tools, record_usage=observed)
    observed.assert_called_once_with(100, 20)
    tools.invoke.assert_not_called()


def test_release_cannot_change_during_incident_retry():
    table = MagicMock()
    store = Ledger("incidents", MagicMock(), table)
    claim = {"PK": "INCIDENT#" + "a" * 32, "lease_owner": "owner", "fencing_token": 2}
    store.begin_execution(claim, runtime.Limits().fingerprint, RELEASE)
    request = table.update_item.call_args.kwargs
    assert "execution_release=:release" in request["ConditionExpression"]
    assert request["ExpressionAttributeValues"][":release"] == RELEASE


@pytest.mark.parametrize("target", ["standalone", "agentcore"])
def test_both_targets_use_owned_permissions_and_no_classic_dependency(target):
    from infra.spec import ROOT, load
    from scripts.dev.validate_durable import examples

    spec = load(ROOT / "examples/deployment.example.json")
    config = json.loads((ROOT / "examples/durable.example.json").read_text())
    stages = examples(spec, {**config, "runtime_target": target})
    text = json.dumps(stages)
    assert "bedrock:InvokeAgent" not in text and "AWS::Bedrock::Agent" not in text
    policies = stages["durable-runtime"]["Resources"]["InvestigateRole"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"]
    actions = [p["Action"] for p in policies]
    env = stages["durable-runtime"]["Resources"]["Investigate"]["Properties"]["Environment"]["Variables"]
    assert env["RUNTIME_TARGET"] == target
    assert stages["routing"]["Resources"]["WorkMapping"]["Properties"]["Enabled"] is False
    if target == "agentcore":
        assert "bedrock-agentcore:InvokeAgentRuntime" in actions
        assert "LOGS_TOOL_ARN" not in env
        assert not any("bedrock:InvokeModel" in p["Action"] for p in policies)
        code = stages["agentcore-runtime"]["Resources"]["Runtime"]["Properties"]["AgentRuntimeArtifact"][
            "CodeConfiguration"
        ]
        assert code["EntryPoint"] == ["tracaus_agentcore.py"] and code["Code"]["S3"]["VersionId"]
        assert (
            stages["agentcore-endpoint"]["Resources"]["Endpoint"]["Properties"]["AgentRuntimeVersion"] == "1"
        )
    else:
        assert ["bedrock:InvokeModel", "bedrock:CountTokens"] in actions
        assert "lambda:InvokeFunction" in actions


def test_paid_canary_requires_explicit_staging_authorization_before_aws():
    from infra.deploy.owned_ops import canary
    from infra.deploy.verify import VerificationError

    factory = MagicMock()
    with pytest.raises(VerificationError):
        canary({"spec": {"environment": "staging"}}, factory)
    with pytest.raises(VerificationError):
        canary({"spec": {"environment": "production"}}, factory, allow_model_invocation=True)
    factory.assert_not_called()


def test_role_verification_rejects_extra_attached_policy():
    from infra.deploy.owned_ops import verify_role
    from infra.deploy.verify import VerificationError

    client = MagicMock()
    trust = {"Version": "2012-10-17", "Statement": []}
    client.get_role.return_value = {"Role": {"AssumeRolePolicyDocument": trust, "Path": "/"}}
    client.list_role_policies.return_value = {"PolicyNames": ["runtime"]}
    client.list_attached_role_policies.return_value = {"AttachedPolicies": [{"PolicyArn": "unexpected"}]}
    with pytest.raises(VerificationError, match="Unexpected execution role policies"):
        verify_role(
            client, "arn:aws:iam::123456789012:role/test", {"AssumeRolePolicyDocument": trust, "Path": "/"}
        )


def test_unsupported_remote_endpoint_denied_before_client():
    with pytest.raises(ValueError):
        agentcore.invoke(
            {"release": RELEASE},
            arn=ARN,
            qualifier="DEFAULT",
            region=REGION,
            account=ACCOUNT,
            session_id="incident_" + "a" * 40,
            deadline=time.time() + 180,
        )


def test_remote_attempt_exception_waits_for_lease_recovery(monkeypatch):
    from unittest.mock import patch

    from tracaus import pipeline

    monkeypatch.setenv("RUNTIME_TARGET", "agentcore")
    monkeypatch.setenv("REPORT_BUCKET", "synthetic-reports")
    monkeypatch.setenv("REPORT_KMS_KEY_ARN", "synthetic-key")
    store, s3 = MagicMock(), MagicMock()
    current = {
        "PK": "INCIDENT#" + "a" * 32,
        "status": "PENDING",
        "deadline_epoch": int(time.time()) + 600,
        "work_intent_sk": "INTENT#WORK#1",
    }
    store.get.side_effect = [current, {"event": {"instance_id": IID}}]
    store.claim.return_value = {
        **current,
        "lease_until": int(time.time()) + 450,
        "attempts": 1,
        "event_id": "a" * 64,
        "fencing_token": 1,
    }
    with (
        patch.object(pipeline, "ledger", return_value=store),
        patch.object(pipeline, "clients", return_value=s3),
        patch.object(pipeline, "invoke_agent", side_effect=TimeoutError("ambiguous completion")),
    ):
        result = pipeline.work(
            {"Records": [{"messageId": "lost", "body": current["PK"] + "|INTENT#WORK#1"}]}, None
        )
    assert result == {"batchItemFailures": [{"itemIdentifier": "lost"}]}
    store.retry.assert_not_called()
    store.complete.assert_not_called()
    s3.put_object.assert_not_called()


def test_expired_retention_is_denied_before_any_report_read(monkeypatch):
    from unittest.mock import patch

    from tracaus.status import load

    monkeypatch.setenv("INCIDENT_TABLE", "synthetic-table")
    monkeypatch.setenv("MONITOR_REGION", REGION)
    resource = MagicMock()
    resource.Table.return_value.get_item.return_value = {"Item": {"ttl": 1, "report_version": "v1"}}
    with patch("boto3.resource", return_value=resource), patch("boto3.client") as client:
        assert load("a" * 32) is None
    client.assert_not_called()


def test_discovery_and_empty_metrics_are_not_completed_incident_evidence():
    client, tools = MagicMock(), MagicMock()
    client.converse.side_effect = [
        answer(
            stop="tool_use",
            blocks=[
                {"toolUse": {"toolUseId": "logs", "name": "fetch_logs", "input": {"instance_id": IID}}},
                {"toolUse": {"toolUseId": "metrics", "name": "fetch_metrics", "input": {"instance_id": IID}}},
            ],
        ),
        answer(),
    ]
    tools.invoke.side_effect = [
        ({"status": "log_groups_found", "complete": True}, True),
        ({"status": "no_data", "complete": True}, True),
    ]
    result = drive(client, tools=tools, require_evidence=True)[0]
    assert result["complete"] is False and result["code"] == "INCOMPLETE_EVIDENCE"
    assert result["tools"] == ["fetch_logs", "fetch_metrics"] and result["evidence"] == []


def test_shared_loop_checkpoint_retains_tool_evidence_before_next_model():
    client, tools, checkpoint = MagicMock(), MagicMock(), MagicMock()
    client.converse.side_effect = [
        answer(
            stop="tool_use",
            blocks=[{"toolUse": {"toolUseId": "logs", "name": "fetch_logs", "input": {"instance_id": IID}}}],
        ),
        answer(),
    ]
    tools.invoke.return_value = ({"status": "no_log_groups_found", "complete": True}, True)
    drive(client, tools=tools, checkpoint=checkpoint)
    assert "Tool evidence: fetch_logs" in checkpoint.call_args_list[0].args[0]


@pytest.mark.parametrize("target", ["standalone", "agentcore"])
def test_chat_adapter_dispatches_selected_target_with_same_versioned_contract(monkeypatch, target):
    from unittest.mock import patch

    from tracaus import chat
    from tracaus.config import AppConfig

    settings = AppConfig(
        REGION,
        "synthetic-password-for-tests",
        runtime_target=target,
        model_id="model-v1",
        account_id=ACCOUNT,
        allowed_ids=IID,
        runtime_release=RELEASE,
        runtime_limits=json.dumps(asdict(runtime.Limits())),
        logs_arn=f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:logs:3",
        metrics_arn=f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:metrics:4",
        agentcore_arn=ARN,
        agentcore_endpoint="release001",
    )
    assert settings.problems() == []
    result = {"version": 1, "text": "Evidence", "complete": True, "release": RELEASE}
    with (
        patch.object(execution, "execute", return_value=result) as direct,
        patch.object(agentcore, "invoke", return_value=result) as remote,
    ):
        assert (
            chat.invoke(
                "Investigate", "a" * 36, settings, history=[{"role": "user", "content": "Earlier"}]
            ).status
            == "ok"
        )
    chosen = direct if target == "standalone" else remote
    unused = remote if target == "standalone" else direct
    unused.assert_not_called()
    assert chosen.call_args.args[0] == {
        "version": 1,
        "release": RELEASE,
        "mode": "chat",
        "prompt": "Investigate",
        "history": [{"role": "user", "content": "Earlier"}],
    }


def test_agentcore_http_contract_serializes_json_as_chunked_sse():
    from email.message import Message
    from unittest.mock import patch

    handler = object.__new__(agentcore_host.Handler)
    handler.path = "/invocations"
    payload = b'{"version":1}'
    handler.headers = Message()
    handler.headers["Content-Length"] = str(len(payload))
    handler.headers["Content-Type"] = "application/json"
    handler.rfile, handler.wfile = BytesIO(payload), BytesIO()
    handler.connection = MagicMock()
    handler.send_response, handler.send_header, handler.end_headers = MagicMock(), MagicMock(), MagicMock()
    with patch.object(agentcore_host, "events", return_value=iter([b"data: {}\n\n"])):
        handler.do_POST()
    handler.send_response.assert_called_once_with(200)
    assert handler.wfile.getvalue() == b"a\r\ndata: {}\n\n\r\n0\r\n\r\n"


def test_runtime_verification_uses_injected_clients_for_stack_checks(monkeypatch):
    from infra.deploy import durable_ops
    from infra.deploy.verify import VerificationError
    from infra.spec import ROOT, load, tags

    spec = load(ROOT / "examples/deployment.example.json")
    spec["reference_only"] = False  # Only injected mocks are permitted below.
    sts, cfn, factory = MagicMock(), MagicMock(), MagicMock()
    sts.get_caller_identity.return_value = {"Account": spec["account_id"]}
    cfn.describe_stacks.return_value = {
        "Stacks": [
            {
                "StackStatus": "CREATE_COMPLETE",
                "EnableTerminationProtection": False,
                "Tags": [{"Key": k, "Value": v} for k, v in tags(spec).items()],
            }
        ]
    }
    factory.side_effect = lambda service, region: {"sts": sts, "cloudformation": cfn}[service]
    monkeypatch.setattr(
        durable_ops, "clients", MagicMock(side_effect=AssertionError("unexpected AWS client"))
    )
    with pytest.raises(VerificationError, match="sealed"):
        durable_ops.verify_runtime({"spec": spec}, factory=factory)
    cfn.describe_stacks.assert_called_once()
    durable_ops.clients.assert_not_called()


@pytest.mark.parametrize(
    "operation,outcome", [("count_tokens", "COUNT_FAILED"), ("converse", "INFERENCE_FAILED")]
)
def test_model_api_failure_has_safe_correlated_telemetry(operation, outcome, capsys):
    from tracaus.telemetry import correlate

    client = MagicMock()
    getattr(client, operation).side_effect = RuntimeError("sensitive remote details")
    with correlate("a" * 32, 2), pytest.raises(RuntimeError):
        drive(client)
    events = [json.loads(s) for s in capsys.readouterr().out.splitlines()]
    assert events == [
        {"Component": "model", "outcome": outcome, "Failure": 1, "incident_id": "a" * 32, "fence": 2}
    ]


def call(name, ident):
    return answer(
        stop="tool_use",
        blocks=[{"toolUse": {"toolUseId": ident, "name": name, "input": {"instance_id": IID}}}],
    )


def test_progress_reports_each_step_with_fixed_names_only():
    tools = MagicMock()
    tools.invoke.return_value = ({"status": "ok", "instance_id": IID, "complete": True}, True)
    client = MagicMock()
    client.converse.side_effect = [call("fetch_logs", "1"), call("fetch_metrics", "2"), answer("Final")]
    steps = []
    result, _, _ = drive(client, tools=tools, progress=steps.append)
    assert result["text"] == "Final"
    assert steps == ["thinking", "tool:fetch_logs", "thinking", "tool:fetch_metrics", "thinking"]


def test_an_unknown_tool_name_is_reported_generically():
    tools = MagicMock()
    tools.invoke.side_effect = runtime.RuntimeStop("UNKNOWN_TOOL")
    client = MagicMock()
    client.converse.side_effect = [call("PRIVATE-NAME-FROM-THE-MODEL", "1")]
    steps = []
    result, _, _ = drive(client, tools=tools, progress=steps.append)
    assert result["code"] == "UNKNOWN_TOOL"
    assert steps == ["thinking", "tool"] and "PRIVATE" not in repr(steps)


def test_a_failing_progress_observer_never_changes_the_investigation():
    def broken(step):
        raise RuntimeError("UI went away")

    client = MagicMock()
    client.converse.return_value = answer("Final")
    result, _, _ = drive(client, progress=broken)
    assert result["complete"] and result["text"] == "Final"
