from unittest.mock import MagicMock, patch

import pytest

from tests.helpers import body, load_lambda
from tests.test_contracts_and_metrics import check_contract
from tracaus import cursor
from tracaus.transport import MAX_ENVELOPE_BYTES, bounded_envelope, dumps

logs = load_lambda("fetch_logs")
IID = "i-0123456789abcdef0"


def event(token=None):
    params = [{"name": "instance_id", "value": IID}]
    if token:
        params.append({"name": "next_token", "value": token})
    return {
        "actionGroup": "logs",
        "apiPath": "/fetch_cloudwatch_logs",
        "httpMethod": "GET",
        "parameters": params,
    }


def test_300_long_group_names_are_all_reachable_with_full_envelope_budget():
    names = [f"/aiops/{IID}/service-{i:04d}".ljust(512, "x") for i in range(300)]
    client = MagicMock()

    def page(**kwargs):
        start = int(kwargs.get("nextToken", "0"))
        end = start + kwargs["limit"]
        return {
            "logGroups": [{"logGroupName": name} for name in names[start:end]],
            **({"nextToken": str(end)} if end < len(names) else {}),
        }

    client.describe_log_groups.side_effect = page
    collected, token = [], None
    with patch("boto3.client", return_value=client):
        for _ in range(20):
            response = logs.lambda_handler(event(token), None)
            check_contract("fetch_logs", response)
            assert len(dumps(response).encode("utf-8")) <= MAX_ENVELOPE_BYTES
            result = body(response)
            collected.extend(result["log_groups"])
            if result["complete"]:
                break
            token = result["next_token"]
        else:
            pytest.fail("Discovery never completed")
    assert collected == names and len(collected) == len(set(collected))


@pytest.mark.parametrize("change", ["instance", "prefix", "region", "environment", "account"])
def test_cursor_cannot_cross_scope(change):
    scope = cursor.scope(IID, "/aiops", "eu-central-1")
    token = cursor.encode("native-page-2", scope)
    with pytest.raises(ValueError):
        cursor.decode(token, {**scope, change: "different"})


def test_cursor_tamper_expiration_and_rotation(monkeypatch):
    scope = cursor.scope(IID, "/aiops", "eu-central-1")
    with patch("tracaus.cursor.time.time", return_value=100):
        token = cursor.encode("page", scope)
        assert cursor.decode(token, scope) == "page"
        with pytest.raises(ValueError):
            cursor.decode("!!" + token, scope)
    with patch("tracaus.cursor.time.time", return_value=3701), pytest.raises(ValueError):
        cursor.decode(token, scope)
    monkeypatch.setenv("LOG_CURSOR_SECRET", "another-synthetic-key-for-testing-rotation")
    with pytest.raises(ValueError):
        cursor.decode(token, scope)


def test_empty_page_with_continuation_is_not_complete():
    client = MagicMock()
    client.describe_log_groups.return_value = {"logGroups": [], "nextToken": "page-two"}
    with patch("boto3.client", return_value=client):
        result = body(logs.lambda_handler(event(), None))
    assert result["complete"] is False and result["next_token"]


def test_empty_discovery_contract():
    client = MagicMock()
    client.describe_log_groups.return_value = {"logGroups": []}
    with patch("boto3.client", return_value=client):
        response = logs.lambda_handler(event(), None)
    check_contract("fetch_logs", response)
    assert body(response)["status"] == "no_log_groups_found" and body(response)["complete"]


def test_oversized_error_and_metadata_still_fit():
    response = bounded_envelope(
        {"actionGroup": "界" * 100000, "apiPath": "🙂" * 10000}, {"status": "error", "message": "界" * 100000}
    )
    assert len(dumps(response).encode()) <= MAX_ENVELOPE_BYTES
    assert body(response)["error_code"] == "RESPONSE_TOO_LARGE"
    check_contract("fetch_logs", response)


def test_unicode_budget_also_fits_ascii_escaped_runtime_serialization():
    import json

    response = bounded_envelope(
        {},
        {
            "status": "ok",
            "complete": True,
            "truncated": False,
            "lines": [{"timestamp": str(i), "message": "🙂界" * 250} for i in range(40)],
        },
    )
    assert len(json.dumps(response).encode("utf-8")) <= MAX_ENVELOPE_BYTES
    assert body(response)["status"] == "partial"
