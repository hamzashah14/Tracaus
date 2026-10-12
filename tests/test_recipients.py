"""One list of addresses receives reports; the fallback address is optional."""

import json
from unittest.mock import MagicMock

import pytest

from infra import durable, reconcile
from infra.spec import digest, fallback_recipients, load, recipients
from infra.verify import VerificationError
from scripts.dev import validate_durable, validate_observations
from tests.helpers import ROOT
from tracaus import observability

ONE = "operator@example.invalid"
A, B, C = "a-team@example.invalid", "b-team@example.invalid", "c-team@example.invalid"
FALLBACK = "fallback@example.invalid"
CONFIG = json.loads((ROOT / "examples/durable.example.json").read_text())
NO_FALLBACK = {k: v for k, v in CONFIG.items() if k != "fallback_email"}


def spec_file(tmp_path, name="deployment", **changes):
    value = json.loads((ROOT / f"examples/{name}.example.json").read_text())
    value.update(changes)
    for key in [k for k, v in changes.items() if v is None]:
        del value[key]
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(value))
    return path


def listed(tmp_path, *addresses, name="deployment"):
    return load(spec_file(tmp_path, name, notification_email=None, notification_emails=list(addresses)))


# ---- the setting itself -----------------------------------------------------------


def test_the_single_address_form_still_works(tmp_path):
    spec = load(spec_file(tmp_path))
    assert recipients(spec) == [ONE]


def test_a_list_is_accepted_and_sorted(tmp_path):
    assert recipients(listed(tmp_path, C, A, B)) == [A, B, C]


@pytest.mark.parametrize(
    "changes",
    [
        {"notification_emails": [A]},  # both forms at once
        {"notification_email": None},  # neither form
        {"notification_email": None, "notification_emails": []},
        {"notification_email": None, "notification_emails": [A, A]},
        {"notification_email": None, "notification_emails": ["a@b.invalid", "c@d.invalid"] * 3},
        {"notification_email": None, "notification_emails": ["not an address"]},
        {"notification_email": None, "notification_emails": ["a,b@example.invalid"]},
        {"notification_email": None, "notification_emails": "a@example.invalid"},
    ],
    ids=["both", "neither", "empty", "duplicate", "too-many", "malformed", "comma", "not-a-list"],
)
def test_invalid_recipient_settings_are_rejected(tmp_path, changes):
    with pytest.raises(ValueError):
        load(spec_file(tmp_path, **changes))


def test_five_recipients_are_the_most(tmp_path):
    five = [f"person{n}@example.invalid" for n in range(5)]
    assert recipients(listed(tmp_path, *five)) == five
    with pytest.raises(ValueError):
        listed(tmp_path, *five, "six@example.invalid")


def test_fallback_defaults_to_the_report_recipients(tmp_path):
    spec = listed(tmp_path, B, A)
    assert fallback_recipients(spec, NO_FALLBACK) == [A, B]
    assert fallback_recipients(spec, CONFIG) == [FALLBACK]


def test_runtime_config_may_omit_the_fallback_or_reuse_the_primary_address(tmp_path):
    spec = load(spec_file(tmp_path))
    for config in (NO_FALLBACK, {**CONFIG, "fallback_email": ONE}):
        (tmp_path / "runtime.json").write_text(json.dumps(config))
        assert durable.load_config(tmp_path / "runtime.json", spec)["runtime_target"]


@pytest.mark.parametrize("address", ["a,b@example.invalid", "no-at-sign", 5])
def test_a_bad_fallback_address_is_still_rejected(tmp_path, address):
    spec = load(spec_file(tmp_path))
    (tmp_path / "runtime.json").write_text(json.dumps({**CONFIG, "fallback_email": address}))
    with pytest.raises(ValueError):
        durable.load_config(tmp_path / "runtime.json", spec)


# ---- what gets deployed -------------------------------------------------------------


def email_resources(template, prefix):
    return {
        logical: resource["Properties"]["Endpoint"]
        for logical, resource in template["Resources"].items()
        if logical.startswith(prefix) and resource["Type"] == "AWS::SNS::Subscription"
    }


def test_the_routing_stack_subscribes_every_recipient(tmp_path):
    routing = validate_durable.examples(listed(tmp_path, B, A), CONFIG)["routing"]
    assert email_resources(routing, "Email") == {
        "Email" + digest(A)[:16]: A,
        "Email" + digest(B)[:16]: B,
    }


def test_a_single_recipient_keeps_its_original_logical_ids(tmp_path):
    stages = validate_durable.examples(load(spec_file(tmp_path)), CONFIG)
    assert email_resources(stages["routing"], "Email") == {"Email" + digest(ONE)[:16]: ONE}
    assert email_resources(stages["durable-foundation"], "Fallback") == {"FallbackRecipient": FALLBACK}


def test_the_fallback_topics_use_the_reports_list_when_no_fallback_is_given(tmp_path):
    spec = listed(tmp_path, B, A, name="observability")
    stages = validate_observations.fixtures(spec, NO_FALLBACK)
    expected = {"FallbackRecipient": A, "FallbackRecipient" + digest(B)[:16]: B}
    assert email_resources(stages["durable-foundation"], "Fallback") == expected
    expected = {"EscalationRecipient": A, "EscalationRecipient" + digest(B)[:16]: B}
    assert email_resources(stages["observation-foundation"], "Escalation") == expected


def test_an_explicit_fallback_replaces_the_list_on_the_fallback_topics(tmp_path):
    spec = listed(tmp_path, A, B, name="observability")
    stages = validate_observations.fixtures(spec, CONFIG)
    assert email_resources(stages["durable-foundation"], "Fallback") == {"FallbackRecipient": FALLBACK}
    assert email_resources(stages["observation-foundation"], "Escalation") == {
        "EscalationRecipient": FALLBACK
    }


def test_the_observers_receive_every_address(tmp_path):
    spec = listed(tmp_path, B, A, name="observability")
    environments = [
        resource["Properties"]["Environment"]["Variables"]
        for stage in validate_observations.fixtures(spec, NO_FALLBACK).values()
        for resource in stage["Resources"].values()
        if resource["Type"] == "AWS::Lambda::Function"
        and "PRIMARY_EMAIL" in resource["Properties"].get("Environment", {}).get("Variables", {})
    ]
    assert environments and all(
        e["PRIMARY_EMAIL"] == f"{A},{B}" and e["FALLBACK_EMAIL"] == f"{A},{B}" for e in environments
    )


# ---- retirement keeps every current recipient -----------------------------------------


def owned(spec, endpoint, arn="arn:aws:sns:eu-central-1:123456789012:t:s"):
    from infra.spec import topic_arn

    return {
        "type": "AWS::SNS::Subscription",
        "id": arn,
        "topic": topic_arn(spec, "reports"),
        "endpoint": endpoint,
        "protocol": "email",
    }


def test_retirement_keeps_every_current_recipient_and_removes_others(tmp_path):
    spec = listed(tmp_path, A, B)
    plan = reconcile.plan(
        spec,
        [owned(spec, A, "arn:1"), owned(spec, B, "arn:2"), owned(spec, "former@example.invalid", "arn:3")],
        "worker",
    )
    assert [item["id"] for item in plan["unsubscribe"]] == ["arn:3"]


# ---- the observer checks every address -------------------------------------------------


def subscriptions(*addresses, confirmed=True):
    return [
        {
            "Protocol": "email",
            "Endpoint": address,
            "SubscriptionArn": f"arn:aws:sns:eu-central-1:123456789012:t:{n}"
            if confirmed
            else "PendingConfirmation",
        }
        for n, address in enumerate(addresses)
    ]


def fake_sns(subs, filters=None):
    sns = MagicMock()
    sns.get_paginator.return_value.paginate.return_value = [{"Subscriptions": subs}]
    sns.get_subscription_attributes.side_effect = lambda SubscriptionArn: {
        "Attributes": {"FilterPolicy": (filters or {}).get(SubscriptionArn, "{}")}
    }
    return sns


def test_every_recipient_must_be_confirmed_and_unfiltered(monkeypatch):
    monkeypatch.setenv("REPORTS_TOPIC_ARN", "arn:aws:sns:eu-central-1:123456789012:reports")
    monkeypatch.setenv("PRIMARY_EMAIL", f"{A},{B}")
    assert observability.confirmed_recipient(fake_sns(subscriptions(A, B)))
    assert not observability.confirmed_recipient(fake_sns(subscriptions(A)))  # B missing
    assert not observability.confirmed_recipient(fake_sns(subscriptions(A, B, B)))  # B duplicated
    # An unrelated address is not this check's concern; registration checks reject extra subscribers.
    assert observability.confirmed_recipient(fake_sns(subscriptions(A, B, C)))
    pending = subscriptions(A, B)
    pending[1]["SubscriptionArn"] = "PendingConfirmation"
    assert not observability.confirmed_recipient(fake_sns(pending))
    filtered = subscriptions(A, B)
    assert not observability.confirmed_recipient(
        fake_sns(filtered, {filtered[1]["SubscriptionArn"]: '{"exclude":["true"]}'})
    )


def test_the_fingerprint_binds_the_whole_list():
    topic = "arn:aws:sns:eu-central-1:123456789012:reports"
    assert observability.recipient_fingerprint(topic, ONE) == observability.recipient_fingerprint(topic, ONE)
    assert observability.recipient_fingerprint(topic, f"{A},{B}") != observability.recipient_fingerprint(
        topic, A
    )


# ---- routing verification ------------------------------------------------------------


def test_routing_verification_needs_every_recipient_registered(tmp_path):
    from infra.spec import topic_arn
    from infra.verify import PendingConfirmation, routing_health
    from tests.test_deployment_automation import confirmed_subscriptions, routing_clients

    spec = listed(tmp_path, A, B)
    ingress = "arn:aws:sqs:eu-central-1:123456789012:tracaus-staging-ingress"
    both = confirmed_subscriptions({**spec, "notification_email": A}, ingress) + [
        {
            "TopicArn": topic_arn(spec, "reports"),
            "Protocol": "email",
            "Endpoint": B,
            "SubscriptionArn": "arn:aws:sns:eu-central-1:123456789012:tracaus-staging-reports:second",
        }
    ]
    assert routing_health(spec, routing_clients(spec, both), ingress)["status"] == "PASS"
    only_first = [s for s in both if s["Endpoint"] != B]
    with pytest.raises(VerificationError, match="unexpected or unconfirmed"):
        routing_health(spec, routing_clients(spec, only_first), ingress)
    unclicked = [{**s, "SubscriptionArn": "PendingConfirmation"} if s["Endpoint"] == B else s for s in both]
    with pytest.raises(PendingConfirmation) as waiting:
        routing_health(spec, routing_clients(spec, unclicked), ingress)
    assert waiting.value.recipients == ["notification_email"] and B not in str(waiting.value)
