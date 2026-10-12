"""Owned observation build verification, pinned runtime checks and explicit inbox attestations."""

import hashlib
import json
import re
import time

from infra import durable, observation_templates, owned_ops
from infra.spec import fallback_recipients, log_groups, metric_catalog, name, recipients
from infra.verify import (
    PendingConfirmation,
    VerificationError,
    assert_account,
    awaiting_confirmation,
    verify_function,
)
from scripts.build_lambdas import OBSERVATION_FUNCTIONS, build
from tracaus.ledger import item
from tracaus.observability import recipient_fingerprint


def build_release(spec, output, wheelhouse):
    output.mkdir(parents=True, exist_ok=True)
    (output / "metric-catalog.json").write_text(json.dumps(metric_catalog(spec)))
    (output / "log-scope.json").write_text(json.dumps(log_groups(spec)))
    return build(
        OBSERVATION_FUNCTIONS, output, output / "metric-catalog.json", wheelhouse, output / "log-scope.json"
    )


def checked_build(path, spec):
    if "observability" not in spec:
        raise VerificationError("Observation build requires the declared observation inventory")
    manifest = durable.checked_build(path, expected_functions=OBSERVATION_FUNCTIONS)
    expected = hashlib.sha256(
        json.dumps(metric_catalog(spec), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    scope = hashlib.sha256(json.dumps(log_groups(spec), separators=(",", ":")).encode()).hexdigest()
    if any(
        v["source_files"].get("config/metric-catalog.json") != expected
        or v["source_files"].get("config/log-scope.json") != scope
        for v in manifest["functions"].values()
    ):
        raise VerificationError("Observer catalog/log scope differs from its reviewed inventory")
    return manifest


def validate_versions(spec, versions):
    if set(versions) != {v + "VersionArn" for v in observation_templates.FUNCTIONS.values()}:
        raise VerificationError("Complete observation Lambda version bindings required")
    for function, logical in observation_templates.FUNCTIONS.items():
        expected = f"arn:aws:lambda:{spec['monitor_region']}:{spec['account_id']}:function:{name(spec, function.replace('_', '-'), True)}:"
        if not isinstance(versions[logical + "VersionArn"], str) or not re.fullmatch(
            re.escape(expected) + r"[1-9][0-9]*", versions[logical + "VersionArn"]
        ):
            raise VerificationError("Observer version must belong to this release/account/region")


def verify_runtime(bundle, factory):
    spec = bundle["spec"]
    assert_account(factory("sts", spec["monitor_region"]), spec)
    validate_versions(spec, bundle["bindings"]["observation_versions"])
    planned = owned_ops.sealed(bundle, "observation-runtime", factory)
    for function, logical in observation_templates.FUNCTIONS.items():
        arn = bundle["bindings"]["observation_versions"][logical + "VersionArn"]
        client = factory("lambda", spec["monitor_region"])
        got = verify_function(client, arn, bundle["bindings"]["observation_artifacts"][function])
        properties = planned["Resources"][logical]["Properties"]
        actual = client.get_function_configuration(FunctionName=arn)
        if got["configuration"] != properties["Environment"]["Variables"] or any(
            actual.get(k) != properties[k] for k in ("Timeout", "MemorySize", "Architectures")
        ):
            raise VerificationError("Observation runtime code/configuration/capacity drifted")
        owned_ops.verify_role(
            factory("iam", spec["monitor_region"]),
            actual["Role"],
            planned["Resources"][logical + "Role"]["Properties"],
        )
    return {
        "status": "PASS",
        "scope": "pinned observation runtime only; actual detection/delivery require customer acceptance",
    }


def seed_health(bundle, factory):
    """Explicit customer operation before promotion; never activates schedules."""
    if bundle["spec"]["reference_only"]:
        raise VerificationError("Synthetic reference cannot seed cloud observations")
    from infra.durable_ops import require_reviewed_source

    require_reviewed_source(bundle)
    spec = bundle["spec"]
    if not spec["observability"]["enabled"]:
        return {"status": "DISABLED", "services": [], "scope": "reviewed observation inventory paused"}
    if spec["maintenance_mode"]:
        raise VerificationError("Health bootstrap requires a non-maintenance reviewed inventory")
    verify_runtime(bundle, factory)
    # Production caller supplies a client with the full observer read timeout.
    client = factory("lambda", spec["monitor_region"])
    results = []
    for service in spec["observability"]["services"]:
        response = client.invoke(
            FunctionName=bundle["bindings"]["observation_versions"]["ObserverVersionArn"],
            InvocationType="RequestResponse",
            Payload=json.dumps({"mode": "health", "service_id": service["id"]}).encode(),
        )
        body = response["Payload"]
        try:
            result = json.loads(body.read(8193))
        finally:
            body.close()
        if (
            response.get("FunctionError")
            or response.get("StatusCode") != 200
            or (result.get("mode") != "health" or result.get("status") != "CHECKED")
        ):
            raise VerificationError("Health bootstrap incomplete; promotion remains blocked")
        results.append(service["id"])
    return {
        "status": "SEEDED",
        "services": results,
        "next": "Run strict coverage/canary; metrics may take time to appear",
    }


def verify_registration(bundle, factory):
    """Verify live scheduling/subscription/alarm registrations, never claim delivery."""
    spec, bindings = bundle["spec"], bundle["bindings"]
    assert_account(factory("sts", spec["monitor_region"]), spec)
    validate_versions(spec, bindings["observation_versions"])
    desired = observation_templates.active(
        spec,
        bindings["foundation"],
        bindings["observation_versions"],
        bindings["versions"],
        runtime_target=bundle["config"]["runtime_target"],
        agentcore=bindings.get("agentcore"),
    )["Resources"]
    events = factory("events", spec["monitor_region"])
    for resource_value in desired.values():
        if resource_value["Type"] != "AWS::Events::Rule":
            continue
        properties = resource_value["Properties"]
        rule = events.describe_rule(Name=properties["Name"])
        targets = events.list_targets_by_rule(Rule=properties["Name"])
        if any(rule.get(k) != properties[k] for k in ("State", "ScheduleExpression")) or (
            targets.get("NextToken") or targets.get("Targets") != properties["Targets"]
        ):
            raise VerificationError("Observer schedule or pinned target drifted")
    client = factory("lambda", spec["monitor_region"])
    properties = desired["ReceiptMapping"]["Properties"]
    mappings = [
        entry
        for page in client.get_paginator("list_event_source_mappings").paginate(
            FunctionName=properties["FunctionName"]
        )
        for entry in page["EventSourceMappings"]
    ]
    if (
        len(mappings) != 1
        or any(
            mappings[0].get(k) != properties[k]
            for k in ("EventSourceArn", "BatchSize", "FunctionResponseTypes")
        )
        or mappings[0].get("FunctionArn") != properties["FunctionName"]
        or (
            mappings[0].get("State") != ("Enabled" if properties["Enabled"] else "Disabled")
            or mappings[0].get("FilterCriteria")
        )
    ):
        raise VerificationError("Synthetic recipient consumer mapping drifted")
    cw = factory("cloudwatch", spec["monitor_region"])
    alarms = {
        r["Properties"]["AlarmName"]: r["Properties"]
        for r in desired.values()
        if r["Type"] == "AWS::CloudWatch::Alarm"
    }
    response = cw.describe_alarms(AlarmNames=sorted(alarms))
    actual = {a["AlarmName"]: a for a in response.get("MetricAlarms", [])}
    if response.get("NextToken") or set(actual) != set(alarms):
        raise VerificationError("Required independent alarms missing")
    for alarm_name, planned in alarms.items():
        got = actual[alarm_name]
        for key, value in planned.items():
            if key == "Tags":
                continue
            current = got.get(key)
            if key == "Dimensions":
                current, value = (
                    sorted(current or [], key=lambda d: d["Name"]),
                    sorted(value, key=lambda d: d["Name"]),
                )
            if current != value:
                raise VerificationError("Independent alarm configuration/actions drifted")
        if got.get("InsufficientDataActions"):
            raise VerificationError("Independent alarm has unexpected actions")
    dashboard = desired["Dashboard"]["Properties"]
    if json.loads(cw.get_dashboard(DashboardName=dashboard["DashboardName"])["DashboardBody"]) != json.loads(
        dashboard["DashboardBody"]
    ):
        raise VerificationError("Operations dashboard differs from reviewed inventory")
    sns = factory("sns", spec["monitor_region"])
    pending = []
    for topic, protocol, endpoint, policy, dead in (
        (
            "canary",
            "sqs",
            bindings["foundation"]["IngressQueueArn"],
            {},
            bindings["foundation"]["DeliveryDeadArn"],
        ),
        (
            "reports",
            "sqs",
            observation_templates.queue_arn(spec, "observation-receipts"),
            {"tracaus_canary": ["true"]},
            observation_templates.queue_arn(spec, "observation-dead"),
        ),
        *[("reports", "email", address, {}, None) for address in recipients(spec)],
        *[
            ("observation-fallback", "email", address, {}, None)
            for address in fallback_recipients(spec, bundle["config"])
        ],
    ):
        from infra.spec import topic_arn

        subscriptions = [
            s
            for page in sns.get_paginator("list_subscriptions_by_topic").paginate(
                TopicArn=topic_arn(spec, topic)
            )
            for s in page["Subscriptions"]
        ]
        matches = [s for s in subscriptions if s["Protocol"] == protocol and s["Endpoint"] == endpoint]
        # Reports also has its primary email. Do not silently accept extra fallback/canary recipients.
        if (
            len(matches) != 1
            or (topic != "reports" and len(subscriptions) != len(fallback_recipients(spec, bundle["config"])))
            or not (matches[0]["SubscriptionArn"].startswith("arn:") or awaiting_confirmation(matches[0]))
        ):
            raise VerificationError("Independent subscription missing, duplicated or unconfirmed")
        if awaiting_confirmation(matches[0]):
            # Its attributes cannot be read yet; they are checked on the pass after the click.
            pending.append(
                "fallback_email"
                if topic == "observation-fallback" and "fallback_email" in bundle["config"]
                else "notification_email"
            )
            continue
        attrs = sns.get_subscription_attributes(SubscriptionArn=matches[0]["SubscriptionArn"])["Attributes"]
        if json.loads(attrs.get("FilterPolicy", "{}")) != policy:
            raise VerificationError("Independent recipient filter drifted")
        if protocol == "sqs" and (
            attrs.get("RawMessageDelivery") != "false"
            or json.loads(attrs.get("RedrivePolicy", "{}")) != {"deadLetterTargetArn": dead}
            or attrs.get("FilterPolicyScope", "MessageAttributes") != "MessageAttributes"
        ):
            raise VerificationError("Independent recipient envelope/redrive settings drifted")
    if pending:  # Never a pass, but raised only once nothing else is wrong.
        raise PendingConfirmation(pending)
    return {
        "status": "PASS",
        "scope": "registration only; failure injection and real delivery require customer acceptance",
    }


def attest_email(bundle, factory, notification_id, *, confirm=False):
    if (
        not confirm
        or not isinstance(notification_id, str)
        or not re.fullmatch(r"[0-9a-f]{32}-initial", notification_id)
    ):
        raise VerificationError(
            "Inbox attestation needs the exact received notification ID and explicit confirmation"
        )
    spec = bundle["spec"]
    assert_account(factory("sts", spec["monitor_region"]), spec)
    identity = factory("sts", spec["monitor_region"]).get_caller_identity()["Arn"]
    client = factory("dynamodb", spec["monitor_region"])
    from boto3.dynamodb.types import TypeDeserializer

    decoder = TypeDeserializer()

    def get(pk, sk="META"):
        data = client.get_item(
            TableName=bundle["bindings"]["foundation"]["TableName"],
            Key=item({"PK": pk, "SK": sk}),
            ConsistentRead=True,
        ).get("Item", {})
        return {k: decoder.deserialize(v) for k, v in data.items()}

    iid = notification_id.removesuffix("-initial")
    now = int(time.time())
    incident = get("INCIDENT#" + iid)
    notification = get("INCIDENT#" + iid, "NOTIFICATION#INITIAL")
    if (
        not incident
        or "canary_slot" not in incident
        or incident.get("ttl", 0) <= now
        or notification.get("status") != "PUBLISHER_ACCEPTED"
    ):
        raise VerificationError("Inbox receipt must identify an unexpired published synthetic notification")
    pk = f"CANARY#{incident['canary_slot']}"
    expected = get(pk)
    if (
        expected.get("incident_id") != iid
        or now - int(expected.get("created_at", 0))
        > spec["observability"]["email_receipt_max_age_hours"] * 3600
    ):
        raise VerificationError("Canary expectation is missing or too old for a fresh inbox check")
    from infra.spec import topic_arn

    fingerprint = recipient_fingerprint(topic_arn(spec, "reports"), ",".join(recipients(spec)))
    client.transact_write_items(
        TransactItems=[
            {
                "Update": {
                    "TableName": bundle["bindings"]["foundation"]["TableName"],
                    "Key": item({"PK": pk, "SK": "META"}),
                    "UpdateExpression": "SET email_received_at=:now, email_observer=:owner, recipient_fingerprint=:recipient",
                    "ConditionExpression": "incident_id=:incident AND ttl>:now",
                    "ExpressionAttributeValues": item(
                        {":now": now, ":owner": identity, ":incident": iid, ":recipient": fingerprint}
                    ),
                }
            },
            {
                "Put": {
                    "TableName": bundle["bindings"]["foundation"]["TableName"],
                    "Item": item(
                        {
                            "PK": "OBS#EMAIL",
                            "SK": "META",
                            "email_received_at": now,
                            "incident_id": iid,
                            "operator": identity,
                            "recipient_fingerprint": fingerprint,
                            "ttl": now + 30 * 86400,
                        }
                    ),
                }
            },
        ]
    )
    return {
        "status": "OPERATOR_ATTESTED",
        "scope": "Trusted operator observed this email; not automated inbox delivery proof",
    }
