"""Fail-closed cloud verification. Read operations never become empty-success results."""

import base64
import json
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from infra.spec import (
    alarm_descriptors,
    digest,
    existing_log_groups,
    log_groups,
    name,
    recipients,
    topic_arn,
)


class VerificationError(RuntimeError):
    pass


class PendingConfirmation(VerificationError):
    """Only email subscriptions awaiting a human click stand between a check and a pass."""

    EXIT_CODE = 3  # An operation's exit status for this case; every other failure exits 1.

    def __init__(self, recipients):
        super().__init__("Email subscriptions await confirmation")
        self.recipients = sorted(set(recipients))  # Config key names only, never addresses.


def awaiting_confirmation(subscription):
    """An email subscription whose owner has not clicked the link yet; no other state qualifies."""
    return subscription["Protocol"] == "email" and subscription["SubscriptionArn"] == "PendingConfirmation"


def assert_account(sts, spec):
    if spec["reference_only"]:
        raise VerificationError("Synthetic reference inputs cannot be used for cloud operations")
    if sts.get_caller_identity()["Account"] != spec["account_id"]:
        raise VerificationError("Wrong AWS account; no writes permitted")


def assert_stack_absent(client, stack_name):
    try:
        client.describe_stacks(StackName=stack_name)
    except ClientError as exc:
        # AccessDenied, throttling and arbitrary ValidationError must never be interpreted as absent.
        error = exc.response.get("Error", {})
        if error.get("Code") == "ValidationError" and "does not exist" in error.get("Message", ""):
            return
        raise
    raise VerificationError("Release stack already exists; use a new release_id, never update a candidate")


def assert_concurrency(client, requested):
    if requested is None or requested == 0:
        return
    settings = client.get_account_settings()
    unreserved = settings["AccountLimit"]["UnreservedConcurrentExecutions"]
    if requested > max(0, unreserved - 100):
        raise VerificationError("Reserved concurrency would consume Lambda's required unreserved capacity")


def exact_metric_exists(client, descriptor):
    token = None
    while True:
        request = {"Namespace": descriptor["namespace"], "MetricName": descriptor["metric_name"]}
        if token:
            request["NextToken"] = token
        response = client.list_metrics(**request)
        for metric in response["Metrics"]:
            if {d["Name"]: d["Value"] for d in metric.get("Dimensions", [])} == descriptor["dimensions"]:
                return True
        token = response.get("NextToken")
        if not token:
            return False


def coverage(spec, clients):
    """All required telemetry and filter fixtures must succeed before promotion."""
    checks = []
    assert_account(clients("sts", spec["monitor_region"]), spec)
    assert_concurrency(clients("lambda", spec["monitor_region"]), spec["reserved_concurrency"])
    ec2 = clients("ec2", spec["monitor_region"])
    expected = {item["id"] for item in spec["instances"]}
    response = ec2.describe_instances(InstanceIds=sorted(expected))
    actual = {
        i["InstanceId"]
        for reservation in response["Reservations"]
        for i in reservation["Instances"]
        if i["State"]["Name"] not in {"terminated", "shutting-down"}
    }
    if expected != actual:
        raise VerificationError("Inventory includes missing or terminated instances")
    cw = clients("cloudwatch", spec["monitor_region"])
    for descriptor in alarm_descriptors(spec):
        if descriptor["namespace"].endswith("/Health") and not spec["observability"]["enabled"]:
            checks.append({"id": descriptor["id"], "status": "disabled_by_reviewed_inventory"})
            continue
        if not exact_metric_exists(cw, descriptor):
            raise VerificationError(f"Required metric unavailable: {descriptor['id']}")
        checks.append({"id": descriptor["id"], "status": "present"})
    logs = clients("logs", spec["monitor_region"])
    if any(i["nginx_alarm"] for i in spec["instances"]):
        for kind, fixture in spec["nginx_filters"].items():
            result = logs.test_metric_filter(
                filterPattern=fixture["pattern"], logEventMessages=[fixture["match"], fixture["miss"]]
            )
            if [item["eventNumber"] for item in result["matches"]] != [1]:
                raise VerificationError(
                    f"Required {kind} metric filter failed its positive/negative fixtures"
                )
        if "observability" in spec:
            # Every declared failed-request status must match, bytes/other statuses must not.
            fixture = spec["nginx_filters"]["access"]
            import re

            from tracaus.nginx import access_evidence

            for status in (500, 502, 503, 504):
                sample = re.sub(r'("[^"]+") \d{3} ', rf"\g<1> {status} ", fixture["match"], count=1)
                if access_evidence(sample)["status"] != status or [
                    m["eventNumber"]
                    for m in logs.test_metric_filter(
                        filterPattern=fixture["pattern"], logEventMessages=[sample, fixture["miss"]]
                    )["matches"]
                ] != [1]:
                    raise VerificationError("Access filter does not cover declared failed-request statuses")
    if "observability" in spec:
        for group in log_groups(spec):
            found = logs.describe_log_groups(logGroupNamePrefix=group)["logGroups"]
            if not any(g["logGroupName"] == group for g in found):
                raise VerificationError("Required evidence log group is absent")
    for group in sorted({g for groups in existing_log_groups(spec).values() for g in groups}):
        found = logs.describe_log_groups(logGroupNamePrefix=group)["logGroups"]
        if not any(g["logGroupName"] == group for g in found):
            raise VerificationError("Existing log group not found")
    return {
        "spec_hash": digest(spec),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASS",
        "metrics": checks,
        "observation_mode": "enabled" if spec.get("observability", {}).get("enabled") else "disabled",
        "limit": "Descriptor existence and filter fixtures; not end-to-end detection or notification delivery",
    }


def verify_function(client, arn, artifact):
    if not arn.rsplit(":", 1)[-1].isdigit():
        raise VerificationError("Expected a qualified numeric Lambda version")
    result = client.get_function(FunctionName=arn)["Configuration"]
    wanted = base64.b64encode(bytes.fromhex(artifact["sha256"])).decode()
    if result["CodeSha256"] != wanted or result["Runtime"] != "python3.12" or result["State"] != "Active":
        raise VerificationError("Candidate Lambda code/runtime/state mismatch")
    if result.get("LastUpdateStatus") not in (None, "Successful"):
        raise VerificationError("Candidate Lambda update did not complete")
    return {
        "arn": arn,
        "code_sha256": artifact["sha256"],
        "configuration": result.get("Environment", {}).get("Variables", {}),
    }


def routing_health(
    spec,
    clients,
    ingress_arn,
    *,
    ec2_targets=None,
):
    from infra.templates import service_routing

    expected = service_routing(spec)["Resources"]
    events = clients("events", spec["monitor_region"])
    rule = events.describe_rule(Name=name(spec, "ec2-down"))
    if rule["State"] != ("DISABLED" if spec["maintenance_mode"] else "ENABLED"):
        raise VerificationError("EC2 state rule enablement differs from the intended mode")
    if json.loads(rule["EventPattern"]) != expected["Ec2Down"]["Properties"]["EventPattern"]:
        raise VerificationError("EC2 state rule inventory or event selection differs from the plan")
    result = events.list_targets_by_rule(Rule=name(spec, "ec2-down"))
    wanted_targets = ec2_targets or [{"Id": "alarms", "Arn": topic_arn(spec, "alarms")}]
    if result.get("NextToken") or result["Targets"] != wanted_targets:
        raise VerificationError("EC2 rule target differs from desired routing")
    sns = clients("sns", spec["monitor_region"])
    pending = []
    for topic, protocol, endpoints in (
        ("alarms", "sqs", [ingress_arn]),
        ("reports", "email", recipients(spec)),
    ):
        subs = []
        for page in sns.get_paginator("list_subscriptions_by_topic").paginate(
            TopicArn=topic_arn(spec, topic)
        ):
            subs.extend(page["Subscriptions"])
        if sorted((s["Protocol"], s["Endpoint"]) for s in subs) != sorted(
            [(protocol, endpoint) for endpoint in endpoints]
            + (
                [
                    (
                        "sqs",
                        f"arn:aws:sqs:{spec['monitor_region']}:{spec['account_id']}:{name(spec, 'observation-receipts')}",
                    )
                ]
                if topic == "reports" and "observability" in spec
                else []
            )
        ) or any(not (s["SubscriptionArn"].startswith("arn:") or awaiting_confirmation(s)) for s in subs):
            raise VerificationError("Routing includes an unexpected or unconfirmed subscriber")
        # The endpoints matched exactly, so a pending email here is one of the configured report recipients.
        if any(awaiting_confirmation(s) for s in subs):
            pending.append("notification_email")
    desired = {
        r["Properties"]["AlarmName"]: r["Properties"]
        for r in expected.values()
        if r["Type"] == "AWS::CloudWatch::Alarm"
    }
    alarms = clients("cloudwatch", spec["monitor_region"]).describe_alarms(AlarmNames=sorted(desired))
    actual = {a["AlarmName"]: a for a in alarms.get("MetricAlarms", [])}
    if alarms.get("NextToken") or set(actual) != set(desired):
        raise VerificationError("Required alarm registrations are missing or incomplete")
    for alarm_name, properties in desired.items():
        registered = actual[alarm_name]
        for key, value in properties.items():
            if key == "Tags":
                continue
            current = registered.get(key)
            if key == "Dimensions":
                current = sorted(current or [], key=lambda d: d["Name"])
                value = sorted(value, key=lambda d: d["Name"])
            if current != value:
                raise VerificationError(f"Required alarm configuration differs: {alarm_name}/{key}")
        if registered.get("OKActions", []) != properties.get("OKActions", []) or registered.get(
            "InsufficientDataActions"
        ):
            raise VerificationError("Required alarm has unexpected additional notification actions")
    if pending:  # Never a pass, but raised only once nothing else is wrong.
        raise PendingConfirmation(pending)
    return {"status": "PASS", "scope": "routing registration only; delivery requires a live canary"}
