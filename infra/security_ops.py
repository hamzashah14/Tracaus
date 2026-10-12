"""Private reviewed recipient retirement and evidence erasure; no deployment."""

import argparse
import json
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from infra import durable_ops, owned_runtime
from infra.aws import clients
from infra.spec import digest, fallback_recipients, recipients, topic_arn
from infra.verify import VerificationError, assert_account
from tracaus.governance import Erasure
from tracaus.ledger import DATABASE_CONFIG


def guard(bundle):
    """Reject synthetic bundles and unverified bindings before any private operation."""
    if bundle["spec"]["reference_only"]:
        raise VerificationError("Private operations require a customer bundle, not a synthetic reference")
    owned_runtime.validate_bindings(bundle["spec"], bundle["config"], bundle["bindings"])


def recipient_plan(bundle, factory=clients):
    """Inspect only subscriptions owned by this customer's declared CF stacks."""
    guard(bundle)
    spec = bundle["spec"]
    assert_account(factory("sts", spec["monitor_region"]), spec)
    desired = {
        topic_arn(spec, "reports"): set(recipients(spec)),
        topic_arn(spec, "incident-fallback"): set(fallback_recipients(spec, bundle["config"])),
        topic_arn(spec, "observation-fallback"): set(fallback_recipients(spec, bundle["config"])),
    }
    sns = factory("sns", spec["monitor_region"])
    retire = []
    for stage in ("routing", "durable-foundation", "observation-foundation"):
        if stage not in bundle["stages"]:
            continue
        cfn, stack = durable_ops.owned_stack(spec, stage, factory=factory)
        for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack["StackId"]):
            for resource in page["StackResourceSummaries"]:
                if (
                    resource["ResourceType"] != "AWS::SNS::Subscription"
                    or resource["ResourceStatus"] == "DELETE_COMPLETE"
                ):
                    continue
                arn = resource["PhysicalResourceId"]
                if not arn.startswith(f"arn:aws:sns:{spec['monitor_region']}:{spec['account_id']}:"):
                    raise VerificationError("Resolve unconfirmed/foreign owned subscriptions explicitly")
                try:
                    attrs = sns.get_subscription_attributes(SubscriptionArn=arn)["Attributes"]
                except ClientError as exc:
                    if exc.response["Error"]["Code"] in {"NotFound", "NotFoundException"}:
                        continue
                    raise
                if attrs["Protocol"] != "email":
                    continue
                topic = attrs["TopicArn"]
                if topic not in desired or not arn.startswith(topic + ":"):
                    raise VerificationError("Owned email subscription topic drifted")
                if attrs["Endpoint"] not in desired[topic]:
                    retire.append({"arn": arn, "topic": topic, "endpoint": attrs["Endpoint"], "stage": stage})
    plan = {"bundle_hash": bundle["review_hash"], "retire": sorted(retire, key=lambda r: r["arn"])}
    return {**plan, "change_hash": digest(plan)}


def retire_recipients(bundle, reviewed, factory=clients):
    guard(bundle)
    durable_ops.require_reviewed_source(bundle)
    if recipient_plan(bundle, factory) != reviewed:
        raise VerificationError("Recipient diff changed; review again")
    sns = factory("sns", bundle["spec"]["monitor_region"])
    for row in reviewed["retire"]:
        sns.unsubscribe(SubscriptionArn=row["arn"])
        try:
            sns.get_subscription_attributes(SubscriptionArn=row["arn"])
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"NotFound", "NotFoundException"}:
                continue
            raise
        raise VerificationError("Retired recipient still exists; stop")
    return {
        "status": "RETIRED",
        "subscriptions": len(reviewed["retire"]),
        "cloudformation_update_required": True,
    }


def erasure(bundle, factory=clients):
    guard(bundle)
    spec, foundation = bundle["spec"], bundle["bindings"]["foundation"]
    assert_account(factory("sts", spec["monitor_region"]), spec)
    table = boto3.resource("dynamodb", region_name=spec["monitor_region"], config=DATABASE_CONFIG).Table(
        foundation["TableName"]
    )
    return Erasure(
        table, factory("s3", spec["monitor_region"]), foundation["EvidenceBucket"], spec["account_id"]
    )


def private_write(path, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("erase-plan", "erase-apply", "recipients-plan", "recipients-apply"),
    )
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--review-hash", required=True)
    parser.add_argument("--incident")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        bundle = durable_ops.read_bundle(args.bundle, args.review_hash)
        guard(bundle)
        if args.command == "recipients-plan":
            result = recipient_plan(bundle)
        elif args.command == "recipients-apply":
            if not args.plan or args.plan.stat().st_size > 4000000:
                raise VerificationError("Provide the bounded private recipient plan")
            result = retire_recipients(bundle, json.loads(args.plan.read_text()))
        else:
            if args.command == "erase-apply":
                durable_ops.require_reviewed_source(bundle)
                if not args.plan or args.plan.stat().st_size > 4000000:
                    raise VerificationError("Provide the bounded private reviewed erasure plan")
                result = erasure(bundle).apply(json.loads(args.plan.read_text()))
            else:
                result = erasure(bundle).plan(args.incident or "")
        private_write(args.output, result)
        print(json.dumps({"status": result.get("status", "PLANNED"), "output": str(args.output)}))
    except (VerificationError, BotoCoreError, ClientError, OSError, ValueError, KeyError, TypeError):
        print(
            "Security operation failed; inspect private inputs and the reviewed customer bundle.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
