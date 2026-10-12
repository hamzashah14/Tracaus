import json
import os
import time
from unittest.mock import Mock

import pytest
import streamlit
from streamlit.testing.v1 import AppTest

from tests.helpers import ROOT
from tracaus import chat, runtime, status, team
from tracaus import status as incident_status


@pytest.fixture
def settings(monkeypatch):
    for key, value in {
        "APP_PASSWORD": "synthetic-workspace-password",
        "BEDROCK_REGION": "eu-central-1",
        "RUNTIME_TARGET": "standalone",
        "EXPECTED_ACCOUNT_ID": "123456789012",
        "BEDROCK_MODEL_ID": "fixture-model",
        "RUNTIME_RELEASE": "a" * 64,
        "RUNTIME_LIMITS": json.dumps(runtime.Limits().__dict__),
        "LOGS_TOOL_ARN": "arn:aws:lambda:eu-central-1:123456789012:function:logs:1",
        "METRICS_TOOL_ARN": "arn:aws:lambda:eu-central-1:123456789012:function:metrics:1",
        "ENVIRONMENT": "development",
    }.items():
        monkeypatch.setenv(key, value)


def app(authenticated=True):
    test = AppTest.from_file(str(ROOT / "app.py"), default_timeout=10)
    if authenticated:
        test.session_state["authenticated"] = True
        test.session_state["auth_at"] = time.monotonic()
    return test


def button(test, label):
    return next(item for item in test.button if item.label == label)


def test_missing_password_safe_setup(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    test = app(False).run()
    assert not test.exception and any("APP_PASSWORD" in item.value for item in test.info)
    assert not test.chat_input


def test_missing_runtime_configuration_disables_input(settings, monkeypatch):
    monkeypatch.delenv("BEDROCK_MODEL_ID")
    test = app().run()
    assert not test.exception and test.chat_input[0].disabled
    assert any("not been checked" in item.value for item in test.info)


def test_production_uses_password_login_and_in_process_chat(settings, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    invoke = Mock(return_value=chat.ChatResult("Answer", "ok"))
    monkeypatch.setattr(chat, "invoke", invoke)
    test = app(False).run()
    assert not test.exception and not test.error and test.text_input and not test.chat_input
    assert not any(item.label == "Sign in with SSO" for item in test.button)
    test.text_input[0].input("synthetic-workspace-password")
    button(test, "Open workspace").click().run()
    assert test.session_state["authenticated"] and not test.exception and not test.chat_input[0].disabled
    test.chat_input[0].set_value("Investigate the incident").run()
    assert not test.exception and any("Answer" in item.value for item in test.markdown)
    invoke.assert_called_once()
    assert "access_ticket" not in invoke.call_args.kwargs


def test_login_and_logout(settings):
    test = app(False).run()
    test.text_input[0].input("synthetic-workspace-password")
    button(test, "Open workspace").click().run()
    assert test.session_state["authenticated"] and not test.exception
    button(test, "Sign out").click().run()
    assert not test.session_state["authenticated"] and test.session_state["messages"] == []


@pytest.mark.parametrize(
    "code,message",
    [
        ("CREDENTIALS_UNAVAILABLE", "AWS credentials are unavailable."),
        ("CREDENTIALS_EXPIRED", "Refresh your AWS sign-in."),
        ("AGENT_UNAVAILABLE", "The model or agent is unavailable."),
        ("EMPTY_RESPONSE", "The agent returned no answer."),
    ],
)
def test_ui_safe_failure_and_retry(settings, monkeypatch, code, message):
    invoke = Mock(
        side_effect=[
            chat.ChatResult("", "error", code, message, "test-reference"),
            chat.ChatResult("Recovered answer", "ok"),
        ]
    )
    monkeypatch.setattr(chat, "invoke", invoke)
    test = app().run()
    original_session = test.session_state["session_id"]
    test.chat_input[0].set_value("Investigate the incident").run()
    assert not test.exception and any(item.value == message for item in test.warning)
    assert any(code in item.value for item in test.caption)
    button(test, "Retry in a new conversation").click().run()
    assert test.session_state["session_id"] != original_session
    assert any("Recovered answer" in item.value for item in test.markdown)
    assert invoke.call_count == 2


def test_ui_partial_answer_visible(settings, monkeypatch):
    monkeypatch.setattr(
        chat,
        "invoke",
        lambda *args, **kwargs: chat.ChatResult(
            "Evidence received before interruption",
            "partial",
            "REQUEST_FAILED",
            "Connection interrupted",
            "ref",
        ),
    )
    test = app().run()
    test.chat_input[0].set_value("Investigate").run()
    assert not test.exception
    assert any("Evidence received" in item.value for item in test.markdown)
    assert any("Partial result" in item.value for item in test.caption)


def test_long_history_requires_new_context(settings):
    test = app().run()
    old = test.session_state["session_id"]
    test.session_state["messages"] = [{"role": "user", "content": "question"}] * chat.MAX_HISTORY_MESSAGES
    test.run()
    assert test.chat_input[0].disabled
    button(test, "New conversation").click().run()
    assert test.session_state["messages"] == [] and test.session_state["session_id"] != old
    assert not test.chat_input[0].disabled


def test_hourly_work_limit_survives_new_conversation(settings):
    test = app().run()
    test.session_state["attempts"] = [time.monotonic()] * chat.MAX_REQUESTS_PER_HOUR
    test.run()
    assert test.chat_input[0].disabled
    button(test, "New conversation").click().run()
    assert test.chat_input[0].disabled


def test_expired_session_clears_private_history(settings):
    test = app().run()
    test.session_state["auth_at"] = time.monotonic() - chat.SESSION_SECONDS - 1
    test.session_state["messages"] = [{"role": "user", "content": "private question"}]
    test.run()
    assert not test.session_state["authenticated"] and not test.session_state["messages"]
    assert not test.chat_input


def test_incident_link_requires_sign_in_and_shows_authorized_report(settings, monkeypatch):
    incident_id = "a" * 32
    read = Mock(
        return_value={
            "incident_id": incident_id,
            "instance_id": "i-0123456789abcdef0",
            "occurred_at": "2026-10-05T10:00:00Z",
            "status": "COMPLETE",
            "report": "Redacted evidence from private storage",
        }
    )
    monkeypatch.setattr(status, "load", read)
    signed_out = app(False)
    signed_out.query_params["incident"] = incident_id
    signed_out.run()
    assert not signed_out.exception
    read.assert_not_called()
    signed_in = app()
    signed_in.query_params["incident"] = incident_id
    signed_in.run()
    assert not signed_in.exception
    read.assert_called_once_with(incident_id)
    assert any("Redacted evidence" in item.value for item in signed_in.text_area)


def test_brand_assets_exist_and_are_transparent_pngs():
    from PIL import Image

    for name in ("tracaus-mark.png", "tracaus-logo-white.png", "tracaus-logo-black.png"):
        with Image.open(ROOT / "assets" / name) as image:
            assert image.format == "PNG" and image.mode == "RGBA"


def test_local_tools_mode_enables_chat_without_deployed_bindings(settings, monkeypatch, tmp_path):
    from tests.helpers import write_local_tools

    for key in ("RUNTIME_RELEASE", "LOGS_TOOL_ARN", "METRICS_TOOL_ARN", "ALLOWED_INSTANCE_IDS"):
        monkeypatch.delenv(key)
    monkeypatch.setenv("TRACAUS_LOCAL_TOOLS", write_local_tools(tmp_path))
    invoke = Mock(return_value=chat.ChatResult("Local answer", "ok"))
    monkeypatch.setattr(chat, "invoke", invoke)
    test = app().run()
    assert not test.exception and not test.chat_input[0].disabled
    assert not any(item.value == "Connect your runtime" for item in test.subheader)
    assert any("Local tools" in item.value for item in test.caption)
    assert test.expander[0].label == "Connection details"
    assert "Local tools (this machine's AWS credentials)" in str(test.json[0].value)
    test.chat_input[0].set_value("Investigate the incident").run()
    assert not test.exception and any("Local answer" in item.value for item in test.markdown)
    invoke.assert_called_once()


def test_invalid_local_tools_file_keeps_chat_disabled_and_explains_why(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("TRACAUS_LOCAL_TOOLS", str(tmp_path / "missing.json"))
    test = app().run()
    assert not test.exception and test.chat_input[0].disabled
    assert any("TRACAUS_LOCAL_TOOLS" in item.value for item in test.markdown)


TEAM_IID = "i-0123456789abcdef0"
TEAM_OTHER = "i-0fedcba9876543210"
TEAM_ISSUER = "https://login.example.invalid"


class VerifiedUser(dict):
    """Stands in for st.user after a verified OIDC login."""

    def to_dict(self):
        return dict(self)


def team_text(users, extra=""):
    body = "".join(
        f'\n[[users]]\nsub = "{sub}"\nrole = "{role}"\ninstances = {json.dumps(ids)}\n'
        for sub, role, ids in users
    )
    return f'issuer = "{TEAM_ISSUER}"\n{extra}{body}'


@pytest.fixture
def team_mode(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("ALLOWED_INSTANCE_IDS", f"{TEAM_IID},{TEAM_OTHER}")
    team._CACHE.clear()
    team._HITS.clear()
    path = tmp_path / "team.toml"

    def configure(users=(("user-1", "investigator", [TEAM_IID]),), claims=None, extra="", options=None):
        path.write_text(team_text(users, extra))
        os.chmod(path, 0o600)
        monkeypatch.setenv("TRACAUS_TEAM_FILE", str(path))
        verified = {
            "is_logged_in": True,
            "iss": TEAM_ISSUER,
            "sub": "user-1",
            "auth_time": time.time() - 60,
            "amr": ["mfa"],
            **(claims or {}),
        }
        monkeypatch.setattr(streamlit, "user", VerifiedUser(verified))
        defaults = {"server.trustedUserHeaders": {}, "server.enableXsrfProtection": True, **(options or {})}
        monkeypatch.setattr(streamlit, "get_option", lambda name: defaults.get(name))
        return path

    return configure


def audit_spy(monkeypatch):
    calls = []
    monkeypatch.setattr(team, "audit", lambda *args, **kwargs: calls.append((args, kwargs)))
    return calls


def bump(path):
    info = os.stat(path)
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))


def test_team_investigator_chats_within_their_instances(team_mode, monkeypatch):
    team_mode()
    calls = audit_spy(monkeypatch)
    invoke = Mock(return_value=chat.ChatResult("Answer", "ok", usage={"input_tokens": 3, "output_tokens": 4}))
    monkeypatch.setattr(chat, "invoke", invoke)
    test = app(False).run()
    assert not test.exception and test.chat_input and not test.chat_input[0].disabled
    test.chat_input[0].set_value(f"Why is {TEAM_IID} slow?").run()
    invoke.assert_called_once()
    assert invoke.call_args.kwargs["allowed"] == frozenset({TEAM_IID})
    assert calls[-1][0] == ("user-1", "investigator", None, "chat", "OK")
    assert calls[-1][1] == {"instance_count": 1, "tokens": {"input": 3, "output": 4}}


def test_team_password_is_not_used(team_mode):
    team_mode()
    test = app(False).run()
    assert not test.exception and not test.text_input


@pytest.mark.parametrize(
    "claims,reason",
    [
        ({"sub": "stranger"}, "not_listed"),
        ({"iss": "https://other.example.invalid"}, "issuer"),
        ({"auth_time": time.time() - 9 * 3600}, "expired"),
        ({"amr": ["pwd"]}, "mfa"),
    ],
)
def test_team_refuses_people_who_are_not_allowed(team_mode, monkeypatch, claims, reason):
    team_mode(claims=claims)
    calls = audit_spy(monkeypatch)
    invoke = Mock()
    monkeypatch.setattr(chat, "invoke", invoke)
    test = app(False).run()
    assert not test.exception and not test.chat_input
    assert team.DENIED_MESSAGES[reason] in [item.value for item in test.error]
    invoke.assert_not_called()
    assert [call[0][4] for call in calls] == ["DENIED_" + reason.upper()]


def test_team_viewer_sees_the_input_disabled_and_the_server_side_refuses_too(team_mode, monkeypatch):
    team_mode(users=(("user-1", "viewer", [TEAM_IID]),))
    calls = audit_spy(monkeypatch)
    invoke = Mock()
    monkeypatch.setattr(chat, "invoke", invoke)
    test = app(False).run()
    assert test.chat_input[0].disabled
    # AppTest refuses set_value on a disabled widget and Streamlit discards a forged value for one, so
    # make the widget hand a prompt back anyway to prove the app's own check refuses it.
    monkeypatch.setattr(streamlit, "chat_input", lambda *args, **kwargs: "question")
    test.run()
    invoke.assert_not_called()
    assert [call[0] for call in calls] == [("user-1", "viewer", None, "chat", "DENIED_ROLE")]


def test_team_removed_user_is_refused_on_the_next_request(team_mode):
    path = team_mode(users=(("user-1", "investigator", [TEAM_IID]), ("user-2", "viewer", [TEAM_IID])))
    test = app(False).run()
    assert test.chat_input and test.sidebar.button
    path.write_text(team_text([("user-2", "viewer", [TEAM_IID])]))
    bump(path)
    test.run()
    assert not test.chat_input
    assert not test.sidebar.button  # a removed person must not see the workspace sidebar from the last run
    assert team.DENIED_MESSAGES["not_listed"] in [item.value for item in test.error]


def test_team_file_that_becomes_invalid_stops_the_app(team_mode):
    path = team_mode()
    test = app(False).run()
    assert test.chat_input
    path.write_text("broken = [")
    bump(path)
    test.run()
    assert not test.chat_input
    assert any("team access list cannot be used" in item.value for item in test.error)


@pytest.mark.parametrize(
    "options",
    [{"server.trustedUserHeaders": {"x": 1}}, {"server.enableXsrfProtection": False}],
)
def test_team_refuses_trusted_headers_and_missing_xsrf_protection(team_mode, options):
    team_mode(options=options)
    test = app(False).run()
    assert not test.chat_input
    assert any("XSRF protection" in item.value for item in test.error)


def test_team_incident_link_is_scoped_and_audited_once(team_mode, monkeypatch):
    team_mode()
    calls = audit_spy(monkeypatch)
    load = Mock(return_value={"instance_id": TEAM_IID, "status": "DONE", "incident_id": "a" * 32})
    monkeypatch.setattr(incident_status, "load", load)
    test = app(False)
    test.query_params["incident"] = "a" * 32
    test.run().run()
    assert load.call_args.kwargs == {"allowed": frozenset({TEAM_IID})}
    assert [c[0][3:] for c in calls if c[0][3] == "report"] == [("report", "OK")]


def test_team_incident_outside_the_list_looks_like_a_missing_incident(team_mode, monkeypatch):
    team_mode()
    load = Mock(return_value=None)
    monkeypatch.setattr(incident_status, "load", load)
    test = app(False)
    test.query_params["incident"] = "a" * 32
    test.run()
    assert load.call_args.kwargs == {"allowed": frozenset({TEAM_IID})}
    assert any("not found" in item.value for item in test.info)


def test_team_audit_lines_never_carry_the_prompt(team_mode, monkeypatch):
    team_mode()
    calls = audit_spy(monkeypatch)
    monkeypatch.setattr(chat, "invoke", Mock(return_value=chat.ChatResult("Answer", "ok")))
    test = app(False).run()
    test.chat_input[0].set_value("UNIQUE-MARKER-123 why is it slow?").run()
    assert calls and "UNIQUE-MARKER-123" not in repr(calls)


def test_team_hourly_limit_is_shared_by_a_person_across_sessions(team_mode, monkeypatch):
    team_mode(extra="[limits]\nchat_per_user_per_hour = 1\n")
    invoke = Mock(return_value=chat.ChatResult("Answer", "ok"))
    monkeypatch.setattr(chat, "invoke", invoke)
    first = app(False).run()
    first.chat_input[0].set_value("first").run()
    second = app(False).run()  # a second browser session of the same person
    second.chat_input[0].set_value("second").run()
    assert invoke.call_count == 1
    assert any("hourly" in item.value for item in second.info)


@pytest.mark.parametrize("name", ["KIRA_AUTH_MODE", "CHAT_FUNCTION_ARN", "KIRA_SESSION_TABLE"])
def test_a_connection_from_the_removed_identity_module_stops_the_app(settings, monkeypatch, name):
    monkeypatch.setenv(name, "oidc")
    test = app(False)
    test.query_params["incident"] = "a" * 32
    test.run()
    assert not test.exception and not test.text_input and not test.chat_input
    assert any("removed identity module" in item.value for item in test.error)
    assert not test.info  # not even the incident lookup ran


@pytest.mark.parametrize(
    "name",
    ["KIRA_TEAM_FILE", "KIRA_LOCAL_TOOLS", "ARGUS_TEAM_FILE", "ARGUS_LOCAL_TOOLS", "ARGUS_DIAGNOSTIC_POLICY"],
)
def test_a_setting_with_the_old_project_name_stops_the_app(settings, monkeypatch, name):
    monkeypatch.setenv(name, "/srv/team.toml")
    test = app(False)
    test.query_params["incident"] = "a" * 32
    test.run()
    assert not test.exception and not test.text_input and not test.chat_input
    message = " ".join(item.value for item in test.error)
    assert name in message and "TRACAUS_" in message
    assert "/srv/team.toml" not in message  # names only, never values
    assert not test.info


def test_team_refused_retry_keeps_the_conversation(team_mode, monkeypatch):
    team_mode(extra="[limits]\nchat_per_user_per_hour = 2\n")
    monkeypatch.setattr(chat, "invoke", Mock(return_value=chat.failure("REQUEST_FAILED", "failed")))
    test = app(False).run()
    test.chat_input[0].set_value("first question").run()
    assert test.session_state["messages"][-1]["status"] == "error"
    assert team.admit("user-1", 2)  # the same person uses the second request in another browser session
    before = list(test.session_state["messages"])
    button(test, "Retry in a new conversation").click().run()
    assert test.session_state["messages"] == before
    assert any("hourly investigation limit" in item.value for item in test.info)


def test_chat_reports_live_steps_and_shows_the_cost_of_each_answer(settings, monkeypatch):
    steps = []

    def answer(prompt, session_id, config, **kwargs):
        kwargs["progress"]("tool:fetch_logs")
        steps.append(kwargs["progress"])
        return chat.ChatResult("Answer", "ok", usage={"input_tokens": 3000, "output_tokens": 500})

    monkeypatch.setattr(chat, "invoke", answer)
    test = app(False).run()
    test.text_input[0].input("synthetic-workspace-password")
    button(test, "Open workspace").click().run()
    test.chat_input[0].set_value("Investigate the incident").run()
    assert not test.exception and len(steps) == 1
    assert any("Answer" in item.value for item in test.markdown)
    assert any(
        item.value == "3,500 tokens" or item.value.startswith("3,500 tokens · ") for item in test.caption
    )
