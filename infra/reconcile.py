"""Retire only CloudFormation-owned resources, with a reviewed immutable diff."""

from infra.spec import digest, name, recipients, topic_arn
from infra.verify import VerificationError, assert_account


def desired_names(spec):
    from infra.spec import alarm_descriptors

    return {d["alarm_name"] for d in alarm_descriptors(spec)} | {name(spec, "worker-errors")}


def plan(spec, owned, desired_worker_arn, desired_alarm_names=None):
    """owned is obtained from the exact routing stack, never a prefix-wide account scan."""
    alarms = desired_names(spec) if desired_alarm_names is None else desired_alarm_names
    result = {"spec_hash": digest(spec), "disable_alarms": [], "unsubscribe": [], "preserve": []}
    for item in owned:
        if item["type"] == "AWS::CloudWatch::Alarm" and item["id"] not in alarms:
            if item.get("actions_enabled", True):
                result["disable_alarms"].append(item["id"])
        elif item["type"] == "AWS::SNS::Subscription":
            expected = (
                {desired_worker_arn} if item["topic"] == topic_arn(spec, "alarms") else set(recipients(spec))
            )
            if item["topic"] not in {topic_arn(spec, "alarms"), topic_arn(spec, "reports")}:
                raise VerificationError("Owned subscription references an unexpected topic")
            if item["endpoint"] not in expected:
                if not item["id"].startswith("arn:"):
                    raise VerificationError(
                        "Retired subscription is unconfirmed; resolve ownership before promotion"
                    )
                result["unsubscribe"].append(item)
        else:
            result["preserve"].append(item["id"])
    result["disable_alarms"].sort()
    result["unsubscribe"].sort(key=lambda item: item["id"])
    result["preserve"].sort()
    result["review_hash"] = digest(result)
    return result


def owned_resources(spec, clients):
    cfn = clients("cloudformation", spec["monitor_region"])
    stack = name(spec, "routing")
    try:
        data = cfn.describe_stacks(StackName=stack)["Stacks"][0]
    except Exception as exc:
        from botocore.exceptions import ClientError

        if (
            isinstance(exc, ClientError)
            and exc.response.get("Error", {}).get("Code") == "ValidationError"
            and "does not exist" in exc.response["Error"].get("Message", "")
        ):
            return []
        raise
    actual = {tag["Key"]: tag["Value"] for tag in data.get("Tags", [])}
    if any(
        actual.get(k) != v
        for k, v in {
            "Project": spec["project"],
            "Environment": spec["environment"],
            "ManagedBy": "tracaus-cloudformation",
        }.items()
    ):
        raise VerificationError("Routing stack ownership tags do not match")
    result = []
    for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack):
        for entry in page["StackResourceSummaries"]:
            if entry["ResourceStatus"] == "DELETE_COMPLETE":
                continue
            item = {"type": entry["ResourceType"], "id": entry["PhysicalResourceId"]}
            if item["type"] == "AWS::CloudWatch::Alarm":
                alarm = clients("cloudwatch", spec["monitor_region"]).describe_alarms(
                    AlarmNames=[item["id"]]
                )["MetricAlarms"]
                if len(alarm) != 1:
                    raise VerificationError("Owned alarm disappeared; refresh the resource plan")
                item["actions_enabled"] = alarm[0]["ActionsEnabled"]
            if item["type"] == "AWS::SNS::Subscription":
                if not item["id"].startswith("arn:"):
                    # Only a current recipient may still await its click: the digest-named logical ID fixes
                    # its endpoint. Verification, not this plan, refuses to pass until it is confirmed.
                    awaiting = {"Email" + digest(address)[:16]: address for address in recipients(spec)}
                    if entry["LogicalResourceId"] not in awaiting:
                        raise VerificationError("Cannot reconcile an unconfirmed subscription automatically")
                    item.update(
                        topic=topic_arn(spec, "reports"),
                        endpoint=awaiting[entry["LogicalResourceId"]],
                        protocol="email",
                    )
                    result.append(item)
                    continue
                try:
                    attrs = clients("sns", spec["monitor_region"]).get_subscription_attributes(
                        SubscriptionArn=item["id"]
                    )["Attributes"]
                except Exception as exc:
                    from botocore.exceptions import ClientError

                    if isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") in {
                        "NotFound",
                        "NotFoundException",
                    }:
                        continue  # Already retired by the reviewed cleanup; CFN removes its stale record on update.
                    raise
                item.update(topic=attrs["TopicArn"], endpoint=attrs["Endpoint"], protocol=attrs["Protocol"])
            result.append(item)
    return result


def apply(spec, clients, reviewed, worker_arn, desired_alarm_names=None):
    assert_account(clients("sts", spec["monitor_region"]), spec)
    actual = plan(spec, owned_resources(spec, clients), worker_arn, desired_alarm_names)
    if actual != reviewed:
        raise VerificationError("Retirement diff changed; obtain a new plan before cleanup")
    if actual["disable_alarms"]:
        cw = clients("cloudwatch", spec["monitor_region"])
        cw.disable_alarm_actions(AlarmNames=actual["disable_alarms"])
        response = cw.describe_alarms(AlarmNames=actual["disable_alarms"])
        alarms = response["MetricAlarms"]
        if {a["AlarmName"] for a in alarms} != set(actual["disable_alarms"]) or any(
            a["ActionsEnabled"] for a in alarms
        ):
            raise VerificationError("Retired alarm actions were not disabled; cleanup stopped")
    sns = clients("sns", spec["monitor_region"])
    for item in actual["unsubscribe"]:
        sns.unsubscribe(SubscriptionArn=item["id"])
        # Confirm removal; permission/API failures are not a missing subscription.
        try:
            sns.get_subscription_attributes(SubscriptionArn=item["id"])
        except Exception as exc:
            from botocore.exceptions import ClientError

            if isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") in {
                "NotFound",
                "NotFoundException",
            }:
                continue
            raise
        raise VerificationError("Retired subscriber still exists")
    return {
        "status": "PASS",
        "review_hash": actual["review_hash"],
        "remaining": "CloudFormation routing update removes retired alarms; history/logs are retained",
    }
