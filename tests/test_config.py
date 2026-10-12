import json

import pytest

from tracaus.config import AppConfig
from tracaus.runtime import Limits


@pytest.fixture
def configured(monkeypatch):
    for key, value in {
        "BEDROCK_REGION": "eu-central-1",
        "ENVIRONMENT": "development",
        "RUNTIME_TARGET": "standalone",
        "EXPECTED_ACCOUNT_ID": "123456789012",
        "BEDROCK_MODEL_ID": "fixture-model",
        "ALLOWED_INSTANCE_IDS": "i-0123456789abcdef0",
        "RUNTIME_RELEASE": "a" * 64,
        "RUNTIME_LIMITS": json.dumps(Limits().__dict__),
        "LOGS_TOOL_ARN": "arn:aws:lambda:eu-central-1:123456789012:function:logs:1",
        "METRICS_TOOL_ARN": "arn:aws:lambda:eu-central-1:123456789012:function:metrics:1",
    }.items():
        monkeypatch.setenv(key, value)
    return AppConfig.from_env()


def test_current_runtime_configuration_is_valid(configured):
    assert configured.problems() == []


@pytest.mark.parametrize("target", ["classic", "unknown", ""])
def test_unsupported_runtime_cannot_enable_chat(configured, monkeypatch, target):
    monkeypatch.setenv("RUNTIME_TARGET", target)
    assert "RUNTIME_TARGET must be standalone or agentcore." in AppConfig.from_env().problems()


def test_unsupported_agent_variables_do_not_select_an_unsupported_runtime(monkeypatch):
    monkeypatch.delenv("RUNTIME_TARGET", raising=False)
    monkeypatch.setenv("BEDROCK_AGENT_ID", "ABCDEFGHIJ")
    assert AppConfig.from_env().runtime_target == "standalone"


@pytest.mark.parametrize(
    "key,value",
    [
        ("RUNTIME_RELEASE", "invented"),
        ("EXPECTED_ACCOUNT_ID", "wrong-account"),
        ("ALLOWED_INSTANCE_IDS", "i-bad"),
        ("RUNTIME_LIMITS", '{"tool_calls":999999}'),
        ("LOGS_TOOL_ARN", "arn:aws:lambda:eu-central-1:999999999999:function:logs:1"),
        ("METRICS_TOOL_ARN", "arn:aws:lambda:eu-central-1:123456789012:function:metrics:$LATEST"),
    ],
)
def test_invalid_bindings_are_rejected(configured, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    assert AppConfig.from_env().problems()


def test_standalone_production_keeps_tool_pins(configured, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert AppConfig.from_env().problems() == []
    monkeypatch.setenv("LOGS_TOOL_ARN", "arn:aws:lambda:eu-central-1:123456789012:function:logs:$LATEST")
    assert AppConfig.from_env().problems()


def test_password_is_not_in_configuration_repr(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "fixture-password-value")
    assert "fixture-password-value" not in repr(AppConfig.from_env())


@pytest.mark.parametrize("target", ["classic", "unknown"])
def test_incident_dispatch_rejects_retired_target_before_request_or_aws(monkeypatch, target):
    from unittest.mock import Mock

    from tracaus import execution, pipeline

    request = Mock()
    monkeypatch.setattr(execution, "incident_request", request)
    monkeypatch.setenv("RUNTIME_TARGET", target)
    with pytest.raises(ValueError, match="Unsupported runtime target"):
        pipeline.invoke_agent({}, 100)
    request.assert_not_called()


MODEL_API = json.dumps(
    {
        "protocol": "openai",
        "base_url": "https://api.example.com/v1",
        "secret_arn": "arn:aws:secretsmanager:eu-central-1:123456789012:secret:tracaus-staging/model-api-key-AbCdEf",  # pragma: allowlist secret
        "secret_version": "11111111-2222-3333-4444-555555555555",
    }
)


def test_model_api_setting_is_accepted_for_standalone(configured, monkeypatch):
    monkeypatch.setenv("MODEL_API", MODEL_API)
    assert AppConfig.from_env().problems() == []


@pytest.mark.parametrize(
    "raw", ["not-json", "[]", '{"protocol": "openai"}', MODEL_API.replace("https", "http")]
)
def test_invalid_model_api_setting_is_rejected(configured, monkeypatch, raw):
    monkeypatch.setenv("MODEL_API", raw)
    assert any("MODEL_API" in problem for problem in AppConfig.from_env().problems())


def test_agentcore_rejects_model_api(configured, monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET", "agentcore")
    monkeypatch.setenv("MODEL_API", MODEL_API)
    assert "AgentCore uses a Bedrock model only; remove MODEL_API." in AppConfig.from_env().problems()


@pytest.fixture
def local(configured, monkeypatch, tmp_path):
    from tests.helpers import write_local_tools

    for key in ("RUNTIME_RELEASE", "LOGS_TOOL_ARN", "METRICS_TOOL_ARN", "ALLOWED_INSTANCE_IDS"):
        monkeypatch.delenv(key)
    monkeypatch.setenv("TRACAUS_LOCAL_TOOLS", write_local_tools(tmp_path))
    return AppConfig.from_env()


def test_local_tools_setting_is_read_from_the_environment(configured, monkeypatch):
    assert configured.local_tools == ""
    monkeypatch.setenv("TRACAUS_LOCAL_TOOLS", "/private/local-tools.json")
    assert AppConfig.from_env().local_tools == "/private/local-tools.json"


def test_local_tools_replace_the_release_fingerprint_and_tool_pins(local):
    assert local.problems() == []


def test_deployed_rules_still_apply_when_local_tools_are_unset(configured, monkeypatch):
    for key in ("RUNTIME_RELEASE", "LOGS_TOOL_ARN", "METRICS_TOOL_ARN"):
        monkeypatch.delenv(key)
    assert any("RUNTIME_RELEASE" in item for item in AppConfig.from_env().problems())
    assert any("execution bindings" in item for item in AppConfig.from_env().problems())


@pytest.mark.parametrize(
    "key,value",
    [
        ("BEDROCK_MODEL_ID", ""),
        ("EXPECTED_ACCOUNT_ID", "wrong-account"),
        ("BEDROCK_REGION", ""),
        ("RUNTIME_LIMITS", '{"tool_calls":999999}'),
        ("RUNTIME_LIMITS", ""),
    ],
)
def test_local_tools_keep_the_model_account_and_limit_requirements(local, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    assert AppConfig.from_env().problems()


@pytest.mark.parametrize(
    "key,value",
    [
        ("ENVIRONMENT", "production"),
        ("RUNTIME_TARGET", "agentcore"),
        ("LOGS_TOOL_ARN", "arn:aws:lambda:eu-central-1:123456789012:function:logs:1"),
        ("ALLOWED_INSTANCE_IDS", "i-0aaaaaaaaaaaaaaaa"),
        ("TRACAUS_LOCAL_TOOLS", "/nonexistent/local-tools.json"),
    ],
)
def test_local_tools_are_rejected_outside_local_development(local, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    assert AppConfig.from_env().problems()


def test_team_mode_is_valid_with_the_standalone_runtime(configured, monkeypatch):
    monkeypatch.setenv("TRACAUS_TEAM_FILE", "/srv/tracaus/team.toml")
    assert AppConfig.from_env().problems() == []


def test_team_mode_needs_the_standalone_runtime(configured, monkeypatch):
    monkeypatch.setenv("TRACAUS_TEAM_FILE", "/srv/tracaus/team.toml")
    monkeypatch.setenv("RUNTIME_TARGET", "agentcore")
    assert "Team mode needs RUNTIME_TARGET=standalone: chat runs in the UI process." in (
        AppConfig.from_env().problems()
    )


def test_allowed_instances_come_from_the_inventory(configured):
    assert AppConfig.from_env().allowed_instances() == {"i-0123456789abcdef0"}
