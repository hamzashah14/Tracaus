"""An account-wide daily token budget across all incident investigations."""

import json
import time
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from infra.spec import ROOT, daily_token_budget, load
from tracaus import execution
from tracaus.ledger import Ledger
from tracaus.runtime import DAILY_TOKEN_BUDGET, Limits, RuntimeStop

DAY = 1_791_936_000  # 2026-10-14T00:00:00Z


def refused():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")


class Table:
    """update_item for the one expression reserve_daily sends: the condition and the ADD, as DynamoDB reads them."""

    def __init__(self):
        self.rows, self.calls = {}, []

    def update_item(self, **kwargs):
        self.calls.append(kwargs)
        row = self.rows.setdefault(kwargs["Key"]["PK"], {})
        values = kwargs["ExpressionAttributeValues"]
        if "tokens_reserved" in row and row["tokens_reserved"] > values[":room"]:
            raise refused()
        row["tokens_reserved"] = row.get("tokens_reserved", 0) + values[":tokens"]


def ledger(table):
    return Ledger("t", MagicMock(), table)


def test_a_reservation_counts_against_the_utc_day_and_the_last_one_that_fits_is_allowed():
    table = Table()
    store = ledger(table)
    store.reserve_daily(1_000_000, 2_500_000, now=DAY + 10)
    store.reserve_daily(1_000_000, 2_500_000, now=DAY + 20)
    store.reserve_daily(500_000, 2_500_000, now=DAY + 30)  # exactly fills the day
    assert table.rows == {"BUDGET#2026-10-14": {"tokens_reserved": 2_500_000}}
    with pytest.raises(RuntimeStop, match="DAILY_BUDGET_EXHAUSTED"):
        store.reserve_daily(1, 2_500_000, now=DAY + 40)
    assert table.rows["BUDGET#2026-10-14"]["tokens_reserved"] == 2_500_000  # a refusal costs nothing


def test_the_budget_resets_at_midnight_utc():
    table = Table()
    store = ledger(table)
    store.reserve_daily(2_000_000, 2_000_000, now=DAY + 86399)
    store.reserve_daily(2_000_000, 2_000_000, now=DAY + 86400)  # the next UTC day
    assert sorted(table.rows) == ["BUDGET#2026-10-14", "BUDGET#2026-10-15"]


def test_the_row_expires_by_itself_and_uses_the_table_condition_that_rejects_overspend():
    table = Table()
    ledger(table).reserve_daily(100, 1000, now=DAY)
    call = table.calls[0]
    assert call["ConditionExpression"] == "attribute_not_exists(tokens_reserved) OR tokens_reserved<=:room"
    assert call["ExpressionAttributeValues"][":room"] == 900
    assert call["ExpressionAttributeValues"][":ttl"] == DAY + 3 * 86400  # DynamoDB TTL cleans it up


def test_one_reservation_larger_than_the_whole_budget_is_refused_without_a_write():
    table = Table()
    with pytest.raises(RuntimeStop, match="DAILY_BUDGET_EXHAUSTED"):
        ledger(table).reserve_daily(1001, 1000, now=DAY)
    assert table.calls == []


def test_other_dynamodb_errors_are_not_mistaken_for_an_exhausted_day():
    table = MagicMock()
    table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "x"
    )
    with pytest.raises(ClientError):
        ledger(table).reserve_daily(1, 10, now=DAY)


@pytest.mark.parametrize("tokens,budget", [(0, 10), (-1, 10), (1.5, 10), (True, 10), (1, 0), (1, "10")])
def test_invalid_reservations_are_refused(tokens, budget):
    with pytest.raises(ValueError):
        ledger(Table()).reserve_daily(tokens, budget, now=DAY)


RELEASE = "a" * 64
CLAIM = {
    "PK": "INCIDENT#" + "b" * 32,
    "status": "RUNNING",
    "lease_owner": "11111111-1111-1111-1111-111111111111",
    "fencing_token": 1,
    "lease_until": time.time() + 1000,
    "deadline_epoch": time.time() + 1000,
    "event_id": "c" * 64,
}
EVENT = {
    "actionable": True,
    "instance_id": "i-0123456789abcdef0",
    "occurred_at": "2026-10-14T10:00:00Z",
    "kind": "alarm",
    "state": "ALARM",
}


def incident_run(monkeypatch, daily=None):
    for key, value in {
        "RUNTIME_RELEASE": RELEASE,
        "RUNTIME_LIMITS": json.dumps(Limits().__dict__),
        "BEDROCK_MODEL_ID": "fixture-model",
        "BEDROCK_REGION": "eu-central-1",
        "ALLOWED_INSTANCE_IDS": "i-0123456789abcdef0",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("EXECUTION_PURPOSE", raising=False)
    if daily is None:
        monkeypatch.delenv("DAILY_TOKEN_BUDGET", raising=False)
    else:
        monkeypatch.setenv("DAILY_TOKEN_BUDGET", str(daily))
    store = MagicMock()
    store.get.side_effect = lambda pk, sk="META": CLAIM if pk.startswith("INCIDENT#") else {"event": EVENT}
    seen = {}
    monkeypatch.setattr(execution, "tools", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        execution, "run", lambda *args, **kwargs: seen.update(kwargs) or {"complete": True, "text": "ok"}
    )
    payload = {
        "version": 1,
        "release": RELEASE,
        "mode": "incident",
        "incident_id": "b" * 32,
        "owner": CLAIM["lease_owner"],
        "fence": 1,
        "deadline": time.time() + 400,
    }
    execution.execute(payload, store=store, checkpoint=lambda text: None)
    return store, seen["reserve"]


def test_every_token_reservation_of_an_incident_is_counted_in_the_day_first(monkeypatch):
    store, reserve = incident_run(monkeypatch, daily=1234)
    order = MagicMock()
    order.attach_mock(store.reserve_daily, "daily")
    order.attach_mock(store.reserve, "incident")
    reserve({"tokens_reserved": 900, "model_steps": 1})
    assert [c[0] for c in order.mock_calls] == ["daily", "incident"]
    store.reserve_daily.assert_called_once_with(900, 1234)


def test_the_default_budget_applies_when_the_environment_sets_none(monkeypatch):
    store, reserve = incident_run(monkeypatch)
    reserve({"tokens_reserved": 5})
    store.reserve_daily.assert_called_once_with(5, DAILY_TOKEN_BUDGET)


def test_other_counters_do_not_touch_the_day(monkeypatch):
    store, reserve = incident_run(monkeypatch)
    reserve({"tool_calls": 1})
    store.reserve_daily.assert_not_called()
    store.reserve.assert_called_once()


def test_an_exhausted_day_stops_before_the_incident_is_charged(monkeypatch):
    store, reserve = incident_run(monkeypatch)
    store.reserve_daily.side_effect = RuntimeStop("DAILY_BUDGET_EXHAUSTED")
    with pytest.raises(RuntimeStop):
        reserve({"tokens_reserved": 5, "model_steps": 1})
    store.reserve.assert_not_called()


def test_the_investigation_says_why_it_did_not_run():
    from tests.test_runtime import drive

    class Spent:
        def reserve(self, delta):
            raise RuntimeStop("DAILY_BUDGET_EXHAUSTED")

    result, client, _ = drive(budget=Spent())
    client.converse.assert_not_called()  # no model call is made or paid for
    assert result["complete"] is False and result["code"] == "DAILY_BUDGET_EXHAUSTED"
    assert "daily token budget is used up" in result["text"] and "00:00 UTC" in result["text"]


@pytest.mark.parametrize("value", [0, -5, 1.5, "2000000", True, 10**9 + 1])
def test_the_setting_is_a_whole_positive_number_within_bounds(tmp_path, value):
    spec = json.loads((ROOT / "examples/deployment.example.json").read_text())
    spec["daily_token_budget"] = value
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="daily_token_budget"):
        load(path)


def test_the_setting_defaults_to_two_million_and_reaches_the_runtime_environment(tmp_path):
    from infra.stacks import owned_runtime
    from tests.test_owned_runtime import BASE, SPEC, bindings

    assert DAILY_TOKEN_BUDGET == 2_000_000 and daily_token_budget(SPEC) == 2_000_000
    assert owned_runtime.environment(SPEC, BASE, bindings())["DAILY_TOKEN_BUDGET"] == "2000000"
    custom = {**SPEC, "daily_token_budget": 750_000}
    assert daily_token_budget(custom) == 750_000
    assert owned_runtime.environment(custom, BASE, bindings(custom))["DAILY_TOKEN_BUDGET"] == "750000"
