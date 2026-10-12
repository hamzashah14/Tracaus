import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock, patch

import pytest

from tests.helpers import LOCAL_A, LOCAL_B, write_local_tools
from tracaus import chat, execution, local_tools
from tracaus.config import AppConfig
from tracaus.runtime import LambdaTools, Limits, RuntimeStop

RELEASE = "a" * 64


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    path = write_local_tools(tmp_path)
    monkeypatch.setenv("TRACAUS_LOCAL_TOOLS", path)
    for key, value in {
        "ENVIRONMENT": "development",
        "BEDROCK_REGION": "eu-central-1",
        "BEDROCK_MODEL_ID": "fixture-model",
        "EXPECTED_ACCOUNT_ID": "123456789012",
        "RUNTIME_LIMITS": json.dumps(Limits().__dict__),
        "EXECUTION_PURPOSE": "chat",
    }.items():
        monkeypatch.setenv(key, value)
    for key in (
        "RUNTIME_RELEASE",
        "LOGS_TOOL_ARN",
        "METRICS_TOOL_ARN",
        "ALLOWED_INSTANCE_IDS",
    ):
        monkeypatch.delenv(key, raising=False)
    return local_tools.load(path)


@pytest.fixture
def local(local_env, monkeypatch):
    monkeypatch.setattr(execution, "run", Mock(return_value={"complete": True, "text": "ok"}))
    return local_env


def request(release="local", **extra):
    return {"version": 1, "release": release, "mode": "chat", "prompt": "Investigate", "history": [], **extra}


def test_local_chat_needs_no_deployed_release_and_uses_the_local_adapter(local):
    result = execution.execute(request(), local=local)
    assert result == {"complete": True, "text": "ok", "release": "local"}
    tools = execution.run.call_args.kwargs["tools"]
    assert type(tools) is LambdaTools and isinstance(tools.client, local_tools.LocalLambdaClient)
    assert tools.allowed == {LOCAL_A, LOCAL_B}  # the file's inventory, not ALLOWED_INSTANCE_IDS


def test_local_chat_needs_no_inventory_environment_variable(local):
    assert "ALLOWED_INSTANCE_IDS" not in os.environ and execution.execute(request(), local=local)["complete"]


def test_local_chat_requires_the_local_release_marker(local):
    with pytest.raises(RuntimeStop, match="RELEASE_MISMATCH"):
        execution.execute(request(RELEASE), local=local)
    execution.run.assert_not_called()


def test_incident_mode_cannot_use_local_tools(local):
    payload = {"version": 1, "release": "local", "mode": "incident"}
    store = Mock()
    with pytest.raises(RuntimeStop, match="PURPOSE_MISMATCH"):
        execution.execute(payload, local=local, store=store)
    execution.run.assert_not_called()
    store.get.assert_not_called()


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_only_development_can_use_local_tools(local, monkeypatch, environment):
    monkeypatch.setenv("ENVIRONMENT", environment)
    with pytest.raises(RuntimeStop, match="PURPOSE_MISMATCH"):
        execution.execute(request(), local=local)
    execution.run.assert_not_called()


def test_tools_use_one_inventory_source_per_mode(local, monkeypatch):
    allowed = {LOCAL_B}
    assert execution.tools(Limits(), Mock(), allowed=allowed, local=local).allowed == allowed
    assert execution.tools(Limits(), Mock(), instance=LOCAL_A, local=local).allowed == {LOCAL_A}
    monkeypatch.setenv("LOGS_TOOL_ARN", "arn:aws:lambda:eu-central-1:123456789012:function:logs:1")
    monkeypatch.setenv("METRICS_TOOL_ARN", "arn:aws:lambda:eu-central-1:123456789012:function:metrics:1")
    monkeypatch.setenv("ALLOWED_INSTANCE_IDS", LOCAL_A)
    with patch("boto3.client"):
        deployed = execution.tools(Limits(), Mock())
    assert deployed.arns["fetch_logs"].endswith(":function:logs:1") and deployed.allowed == {LOCAL_A}
    assert not isinstance(deployed.client, local_tools.LocalLambdaClient)


def test_deployed_path_is_unchanged_without_local(local, monkeypatch):
    monkeypatch.setenv("RUNTIME_RELEASE", RELEASE)
    tools = Mock(return_value="tools")
    monkeypatch.setattr(execution, "tools", tools)
    assert execution.execute(request(RELEASE), local=None)["release"] == RELEASE
    assert "local" not in tools.call_args.kwargs
    with pytest.raises(RuntimeStop, match="RELEASE_MISMATCH"):
        execution.execute(request("local"))  # a deployed runtime never accepts the local marker


def test_chat_end_to_end_runs_the_real_handlers_against_fake_aws(local_env):
    def reply(blocks, stop, tokens):
        return {
            "output": {"message": {"role": "assistant", "content": blocks}},
            "stopReason": stop,
            "usage": {"inputTokens": tokens, "outputTokens": 10},
        }

    ask = {
        "toolUseId": "m1",
        "name": "fetch_metrics",
        "input": {"instance_id": LOCAL_A, "metric_id": "a-cpu"},
    }
    bedrock = MagicMock()
    bedrock.count_tokens.return_value = {"inputTokens": 100}
    bedrock.converse.side_effect = [
        reply([{"toolUse": ask}], "tool_use", 50),
        reply([{"text": "CPU peaked at 41.5 percent."}], "end_turn", 60),
    ]
    cloudwatch = MagicMock()
    point = {"Timestamp": datetime.now(timezone.utc), "Average": 41.5, "Unit": "Percent"}
    cloudwatch.get_metric_statistics.return_value = {"Datapoints": [point]}
    clients = {"bedrock-runtime": bedrock, "cloudwatch": cloudwatch}
    with patch("boto3.client", side_effect=lambda service, **kwargs: clients[service]):
        result = chat.invoke(f"CPU on {LOCAL_A}?", "session", AppConfig.from_env())
    assert result.status == "ok" and "41.5" in result.text
    assert cloudwatch.get_metric_statistics.call_args.kwargs["Namespace"] == "AWS/EC2"
    tool_result = bedrock.converse.call_args_list[1].kwargs["messages"][-1]["content"][0]["toolResult"]
    assert tool_result["status"] == "success" and "41.5" in json.dumps(tool_result["content"])
