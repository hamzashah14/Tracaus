"""Team mode passes the person's instance list to the tools; hosts never do."""

import json

import pytest

from tracaus import execution
from tracaus.runtime import Limits, RuntimeStop

IID = "i-0123456789abcdef0"
RELEASE = "a" * 64
CHAT = {"version": 1, "release": RELEASE, "mode": "chat", "prompt": "why?", "history": []}


@pytest.fixture
def host(monkeypatch):
    for key, value in {
        "RUNTIME_RELEASE": RELEASE,
        "RUNTIME_LIMITS": json.dumps(Limits().__dict__),
        "BEDROCK_MODEL_ID": "fixture-model",
        "BEDROCK_REGION": "eu-central-1",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("EXECUTION_PURPOSE", raising=False)
    seen = {}
    monkeypatch.setattr(execution, "tools", lambda policy, reserve, **kwargs: seen.update(kwargs) or object())
    monkeypatch.setattr(
        execution, "run", lambda *args, **kwargs: {"complete": True, "text": "ok", "usage": {}}
    )
    return seen


def test_chat_scope_reaches_the_tools(host):
    execution.execute(CHAT, allowed=frozenset({IID}))
    assert host["allowed"] == frozenset({IID})


def test_chat_without_a_scope_keeps_the_deployment_inventory(host):
    execution.execute(CHAT)
    assert host.get("allowed") is None


def test_a_scope_is_refused_for_incident_runs(host):
    incident = {
        "version": 1,
        "release": RELEASE,
        "mode": "incident",
        "incident_id": "x",
        "owner": "o",
        "fence": 1,
        "deadline": 0,
    }
    with pytest.raises(RuntimeStop, match="PURPOSE_MISMATCH"):
        execution.execute(incident, allowed=frozenset({IID}))


def test_chat_progress_reaches_the_runtime(host, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        execution,
        "run",
        lambda *args, **kwargs: seen.update(kwargs) or {"complete": True, "text": "ok", "usage": {}},
    )

    def report(step):
        return None

    execution.execute(CHAT, progress=report)
    assert seen["progress"] is report
    seen.clear()
    execution.execute(CHAT)
    assert seen["progress"] is None
