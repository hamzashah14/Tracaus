import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, NoCredentialsError

from tracaus import chat, execution, runtime
from tracaus.config import AppConfig

SETTINGS = AppConfig(
    "eu-central-1",
    "",
    model_id="fixture-model",
    account_id="123456789012",
    allowed_ids="i-0123456789abcdef0",
    runtime_release="a" * 64,
    runtime_limits=json.dumps(runtime.Limits().__dict__),
    logs_arn="arn:aws:lambda:eu-central-1:123456789012:function:logs:1",
    metrics_arn="arn:aws:lambda:eu-central-1:123456789012:function:metrics:1",
)


def test_client_construction_failure_handled(monkeypatch):
    monkeypatch.setattr(execution, "execute", Mock(side_effect=NoCredentialsError()))
    result = chat.invoke("investigate", "session", SETTINGS)
    assert result.code == "CREDENTIALS_UNAVAILABLE" and result.reference


def test_default_mode_chat_runs_in_process_without_access_ticket(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    execute = Mock(return_value={"text": "Evidence", "complete": True})
    monkeypatch.setattr(execution, "execute", execute)
    result = chat.invoke("investigate", "session", replace(SETTINGS, environment="production"))
    assert result.status == "ok" and result.text == "Evidence"
    assert "access_ticket" not in execute.call_args.args[0]


@pytest.mark.parametrize(
    "code,expected",
    [
        ("ExpiredTokenException", "CREDENTIALS_EXPIRED"),
        ("AccessDeniedException", "ACCESS_DENIED"),
        ("ResourceNotFoundException", "AGENT_UNAVAILABLE"),
    ],
)
def test_provider_errors_are_safe(monkeypatch, code, expected):
    error = ClientError({"Error": {"Code": code, "Message": "DO-NOT-DISPLAY-secret"}}, "Converse")
    monkeypatch.setattr(execution, "execute", Mock(side_effect=error))
    result = chat.invoke("investigate", "session", SETTINGS)
    assert result.code == expected and "DO-NOT" not in repr(result)


def test_partial_evidence_preserved_on_owned_execution_stop(monkeypatch):
    monkeypatch.setattr(execution, "execute", lambda *args: {"text": "Useful evidence", "complete": False})
    result = chat.invoke("investigate", "session", SETTINGS)
    assert result.status == "partial" and result.text == "Useful evidence"
    assert result.code == "INVESTIGATION_INCOMPLETE"


def test_owned_output_preserves_unicode(monkeypatch):
    monkeypatch.setattr(execution, "execute", lambda *args: {"text": "Evidence: 界🙂", "complete": True})
    result = chat.invoke("investigate", "session", SETTINGS)
    assert result.status == "ok" and result.text == "Evidence: 界🙂"


def test_output_limit_does_not_exceed_display_budget(monkeypatch):
    monkeypatch.setattr(execution, "execute", lambda *args: {"text": "界" * 30000, "complete": True})
    result = chat.invoke("investigate", "session", SETTINGS)
    assert result.code == "OUTPUT_LIMIT" and len(result.text.encode()) <= chat.MAX_OUTPUT_BYTES


@pytest.mark.parametrize("prompt", ["", "   ", "x" * 4001, None])
def test_input_limits_do_not_invoke_runtime(monkeypatch, prompt):
    execute = Mock()
    monkeypatch.setattr(execution, "execute", execute)
    assert chat.invoke(prompt, "session", SETTINGS).code == "INVALID_PROMPT"
    execute.assert_not_called()


def test_bounded_history_and_attempt_window():
    messages = []
    for _ in range(100):
        messages = chat.append_exchange(messages, "question", chat.ChatResult("answer", "ok"))
    assert len(messages) == chat.MAX_HISTORY_MESSAGES
    assert chat.recent_attempts([0, 100, 3601], 3700) == [3601]
    assert len(chat.recent_attempts(list(range(3000, 3100)), 3100)) == 100  # no cap below the hourly limit


def test_unconfigured_runtime_does_not_invoke_aws(monkeypatch):
    execute = Mock()
    monkeypatch.setattr(execution, "execute", execute)
    result = chat.invoke("Investigate", "session", AppConfig("eu-central-1", ""))
    assert result.code == "NOT_CONFIGURED"
    execute.assert_not_called()


def test_deployed_chat_still_calls_execute_with_the_payload_only(monkeypatch):
    execute = Mock(return_value={"text": "Evidence", "complete": True})
    monkeypatch.setattr(execution, "execute", execute)
    assert chat.invoke("investigate", "session", SETTINGS).status == "ok"
    assert execute.call_args.args[0]["release"] == "a" * 64
    assert execute.call_args.kwargs == {}


def test_local_tools_chat_passes_the_loaded_config_and_the_local_release(monkeypatch, tmp_path):
    from tests.helpers import write_local_tools
    from tracaus import local_tools

    for key in ("ALLOWED_INSTANCE_IDS", "LOG_CURSOR_SECRET_ARN"):
        monkeypatch.delenv(key, raising=False)
    path = write_local_tools(tmp_path)
    local = replace(
        SETTINGS, runtime_release="", logs_arn="", metrics_arn="", allowed_ids="", local_tools=path
    )
    execute = Mock(return_value={"text": "Evidence", "complete": True})
    monkeypatch.setattr(execution, "execute", execute)
    assert chat.invoke("investigate", "session", local).status == "ok"
    assert execute.call_args.args[0]["release"] == "local"
    assert execute.call_args.kwargs["local"].instances == local_tools.load(path).instances


def team_settings(monkeypatch):
    import json

    from tracaus.config import AppConfig
    from tracaus.runtime import Limits

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
    for key in ("MODEL_API", "TRACAUS_LOCAL_TOOLS"):
        monkeypatch.delenv(key, raising=False)
    return AppConfig.from_env()


def test_invoke_without_a_scope_calls_execute_exactly_as_before(monkeypatch):
    from unittest.mock import Mock

    from tracaus import chat, execution

    execute = Mock(
        return_value={"text": "ok", "complete": True, "usage": {"input_tokens": 3, "output_tokens": 4}}
    )
    monkeypatch.setattr(execution, "execute", execute)
    result = chat.invoke("question", "session", team_settings(monkeypatch))
    assert result.status == "ok"
    assert execute.call_args.kwargs == {}
    assert result.usage == {"input_tokens": 3, "output_tokens": 4}


def test_invoke_passes_the_callers_scope_to_execute(monkeypatch):
    from unittest.mock import Mock

    from tracaus import chat, execution

    execute = Mock(return_value={"text": "ok", "complete": True, "usage": {}})
    monkeypatch.setattr(execution, "execute", execute)
    chat.invoke("question", "session", team_settings(monkeypatch), allowed={"i-0123456789abcdef0"})
    assert execute.call_args.kwargs == {"allowed": frozenset({"i-0123456789abcdef0"})}


def test_failures_carry_no_usage():
    from tracaus import chat

    assert chat.failure("X", "message").usage == {}


def test_a_scope_is_refused_where_the_runtime_cannot_enforce_it(monkeypatch):
    from tracaus import agentcore

    calls = [Mock(), Mock()]
    monkeypatch.setattr(execution, "execute", calls[0])
    monkeypatch.setattr(agentcore, "invoke", calls[1])
    settings = Mock()
    settings.problems.return_value = []
    settings.runtime_target = "agentcore"
    result = chat.invoke("question", "session", settings, allowed={"i-0123456789abcdef0"})
    assert (result.status, result.code) == ("error", "SCOPE_UNSUPPORTED")
    assert result.message == "This deployment cannot restrict chat to a list of instances."
    for call in calls:
        call.assert_not_called()


def test_progress_and_usage_travel_with_the_chat_request(monkeypatch):
    execute = Mock(
        return_value={"text": "ok", "complete": True, "usage": {"input_tokens": 3, "output_tokens": 4}}
    )
    monkeypatch.setattr(execution, "execute", execute)

    def report(step):
        return None

    result = chat.invoke("question", "session", team_settings(monkeypatch), progress=report)
    assert execute.call_args.kwargs == {"progress": report}
    messages = chat.append_exchange([], "question", result, seconds=2.5)
    assert messages[-1]["usage"] == {"input_tokens": 3, "output_tokens": 4} and messages[-1]["seconds"] == 2.5
