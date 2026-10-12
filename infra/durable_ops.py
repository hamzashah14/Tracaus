"""Reviewed durable pipeline cloud operations; the synthetic reference is never deployable."""

import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from infra import durable, durable_templates, owned_ops, reconcile, release, templates
from infra.aws import clients
from infra.spec import ROOT, alarm_descriptors, digest, name, prefix, tags
from infra.verify import (
    PendingConfirmation,
    VerificationError,
    assert_account,
    assert_concurrency,
    assert_stack_absent,
    routing_health,
    verify_function,
)

IMMUTABLE = {
    "durable-runtime",
    "owned-tools",
    "agentcore-runtime",
    "agentcore-endpoint",
    "observation-runtime",
}
STAGES = {
    "foundation-tools",
    "foundation-monitor",
    "durable-foundation",
    "routing",
    "observation-foundation",
    "observations",
} | IMMUTABLE
TOOLS_REGION = {
    "foundation-tools",
    "owned-tools",
    "agentcore-runtime",
    "agentcore-endpoint",
}


def stage_region(spec, stage):
    return spec["bedrock_region"] if stage in TOOLS_REGION else spec["monitor_region"]


def read_bundle(path, expected_hash):
    bundle = json.loads((path / "bundle.json").read_text())
    review_hash = bundle.pop("review_hash")
    if digest(bundle) != review_hash or review_hash != expected_hash:
        raise VerificationError("Durable bundle differs from the reviewed plan")
    bundle["review_hash"] = review_hash
    spec = bundle["spec"]
    for stage, meta in bundle["stages"].items():
        expected_stack = name(spec, stage, stage in IMMUTABLE)
        if (
            stage not in STAGES
            or meta["region"] != stage_region(spec, stage)
            or meta["create_only"] != (stage in IMMUTABLE)
            or meta["stack"] != expected_stack
        ):
            raise VerificationError("Durable stage ownership differs from the plan")
        template = json.loads((path / f"{stage}.json").read_text())
        if templates.template_hash(template) != meta["template_hash"]:
            raise VerificationError("Durable template differs from the reviewed plan")
    return bundle


def require_reviewed_source(bundle):
    current = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
    if bundle["source_dirty"] or dirty or current != bundle["source_sha"]:
        raise VerificationError("Run cloud mutations from the clean reviewed source revision")


def owned_stack(spec, stage, factory=clients):
    client = factory("cloudformation", stage_region(spec, stage))
    stack_name = name(spec, stage, stage in IMMUTABLE)
    current = client.describe_stacks(StackName=stack_name)["Stacks"][0]
    actual = {item["Key"]: item["Value"] for item in current.get("Tags", [])}
    if any(actual.get(key) != value for key, value in tags(spec).items()):
        raise VerificationError("Durable stack is not owned by this environment")
    if current["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
        raise VerificationError("Durable stack has not completed its change")
    return client, current


def collect(bundle, stage, factory=clients):
    spec = bundle["spec"]
    assert_account(factory("sts", spec["monitor_region"]), spec)
    _, current = owned_stack(spec, stage, factory=factory)
    output = {row["OutputKey"]: row["OutputValue"] for row in current.get("Outputs", [])}
    if stage == "durable-foundation":
        required = {
            "TableArn",
            "TableName",
            "StreamArn",
            "EvidenceBucket",
            "EvidenceKeyArn",
            "StreamDeadArn",
            "DeliveryDeadArn",
        } | {
            key + suffix
            for key in ("Ingress", "Work", "Initial", "Report")
            for suffix in ("QueueArn", "QueueUrl")
        }
    elif stage == "durable-runtime":
        required = {
            n + "VersionArn" for n in ("Ingress", "Dispatch", "Investigate", "Initial", "Report", "Reconcile")
        }
    elif stage == "owned-tools":
        required = {"LogsVersionArn", "MetricsVersionArn"}
    elif stage == "agentcore-runtime":
        required = {"RuntimeArn", "RuntimeId", "RuntimeVersion"}
    elif stage == "agentcore-endpoint":
        required = {"RuntimeArn", "EndpointArn", "EndpointName", "RuntimeVersion"}
    elif stage == "observation-runtime":
        required = {k + "VersionArn" for k in ("Observer", "Canary", "Receipt")}
    else:
        required = set(output)

    if not required <= set(output):
        raise VerificationError("Durable stack outputs are incomplete")
    return {key: output[key] for key in required}


def upload(bundle, build_dir, artifact_kind="pipeline"):
    require_reviewed_source(bundle)
    spec = bundle["spec"]
    region = (
        spec["monitor_region"] if artifact_kind in {"pipeline", "observation"} else spec["bedrock_region"]
    )
    assert_account(clients("sts", region), spec)
    target = {
        "pipeline": "durable-runtime",
        "tools": "owned-tools",
        "host": "agentcore-runtime",
        "observation": "observation-runtime",
    }[artifact_kind]
    assert_stack_absent(clients("cloudformation", region), name(spec, target, True))
    if artifact_kind == "observation":
        from infra.observations import checked_build

        build = checked_build(build_dir, spec)
    elif artifact_kind == "tools":
        build = release.checked_build(build_dir, spec)
    elif artifact_kind == "host":
        build = durable.checked_build(
            build_dir, expected_functions=("incident_investigate",), architecture="arm64"
        )
    else:
        build = durable.checked_build(build_dir)
    bucket = templates.bucket_name(
        spec, "monitor" if artifact_kind in {"pipeline", "observation"} else "tools"
    )
    s3 = clients("s3", region)
    owner = {"Bucket": bucket, "ExpectedBucketOwner": spec["account_id"]}
    if s3.get_bucket_versioning(**owner).get("Status") != "Enabled":
        raise VerificationError("Monitor artifact bucket is not versioned")
    if (s3.get_bucket_location(**owner).get("LocationConstraint") or "us-east-1") != region:
        raise VerificationError("Monitor artifact bucket is in another region")
    result = {}
    for function, entry in build["functions"].items():
        key = f"releases/{spec['release_id']}/{entry['sha256']}.zip"
        response = s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=(build_dir / entry["artifact"]).read_bytes(),
            ExpectedBucketOwner=spec["account_id"],
            ServerSideEncryption="AES256",
            ChecksumSHA256=base64.b64encode(bytes.fromhex(entry["sha256"])).decode(),
        )
        if not response.get("VersionId"):
            raise VerificationError("Pipeline artifact version was not returned")
        result[function] = {
            "bucket": bucket,
            "key": key,
            "version_id": response["VersionId"],
            "sha256": entry["sha256"],
        }
    return result["incident_investigate"] if artifact_kind == "host" else result


def change_set(bundle, directory, stage):
    require_reviewed_source(bundle)
    spec = bundle["spec"]
    region = stage_region(spec, stage)
    assert_account(clients("sts", region), spec)
    if stage == "durable-runtime":
        assert_concurrency(clients("lambda", region), bundle["config"]["initial_reserved_concurrency"])
        verify_capture(bundle)
    cfn = clients("cloudformation", region)
    stack = bundle["stages"][stage]["stack"]
    kind = "CREATE"
    if stage in IMMUTABLE:
        assert_stack_absent(cfn, stack)
    else:
        try:
            assert_stack_absent(cfn, stack)
        except VerificationError:
            owned_stack(spec, stage)
            kind = "UPDATE"
    return cfn.create_change_set(
        StackName=stack,
        ChangeSetName="review-" + bundle["review_hash"][:24],
        ChangeSetType=kind,
        ClientToken=bundle["review_hash"],
        TemplateBody=(directory / f"{stage}.json").read_text(),
        Capabilities=["CAPABILITY_IAM"],
        RoleARN=spec["deployment_role_arn"],
        Tags=templates.tagged(spec),
    )


def verify_runtime(bundle, factory=clients):
    spec = bundle["spec"]
    assert_account(factory("sts", spec["monitor_region"]), spec)
    cfn, stack = owned_stack(spec, "durable-runtime", factory=factory)
    if (
        stack["StackStatus"] != "CREATE_COMPLETE"
        or not stack.get("EnableTerminationProtection")
        or json.loads(cfn.get_stack_policy(StackName=name(spec, "durable-runtime", True))["StackPolicyBody"])
        != release.SEALED_POLICY
    ):
        raise VerificationError("Durable candidate must be sealed before promotion")
    output = collect(bundle, "durable-runtime", factory=factory)
    if output != bundle["bindings"]["versions"]:
        raise VerificationError("Durable runtime version outputs changed")
    planned = json.loads((Path(bundle["directory"]) / "durable-runtime.json").read_text())
    if templates.template_hash(planned) != bundle["stages"]["durable-runtime"]["template_hash"]:
        raise VerificationError("Durable template differs from the reviewed bundle")
    deployed = cfn.get_template(StackName=name(spec, "durable-runtime", True))["TemplateBody"]
    if isinstance(deployed, str):
        deployed = json.loads(deployed)
    if templates.template_hash(deployed) != templates.template_hash(planned):
        raise VerificationError("Sealed durable template differs from the reviewed plan")
    for function, logical in (
        ("incident_ingress", "Ingress"),
        ("incident_dispatch", "Dispatch"),
        ("incident_investigate", "Investigate"),
        ("incident_initial", "Initial"),
        ("incident_report", "Report"),
        ("incident_reconcile", "Reconcile"),
    ):
        artifact = bundle["bindings"]["artifacts"][function]
        arn = output[logical + "VersionArn"]
        result = verify_function(factory("lambda", spec["monitor_region"]), arn, artifact)
        expected = planned["Resources"][logical]["Properties"]["Environment"]["Variables"]
        if result["configuration"] != expected:
            raise VerificationError("Durable function configuration differs from the plan")
        properties = planned["Resources"][logical]["Properties"]
        actual = factory("lambda", spec["monitor_region"]).get_function_configuration(FunctionName=arn)
        role_properties = planned["Resources"][logical + "Role"]["Properties"]
        physical = cfn.describe_stack_resource(
            StackName=name(spec, "durable-runtime", True), LogicalResourceId=logical + "Role"
        )["StackResourceDetail"]["PhysicalResourceId"]
        role_arn = f"arn:aws:iam::{spec['account_id']}:role{role_properties['Path']}{physical}"
        if actual.get("Role") != role_arn:
            raise VerificationError("Durable function role binding differs from its owned stack")
        owned_ops.verify_role(factory("iam", spec["monitor_region"]), role_arn, role_properties)
        if any(actual.get(key) != properties[key] for key in ("Timeout", "MemorySize", "Architectures")):
            raise VerificationError("Durable function runtime capacity or deadline differs from the plan")
        reserved = (
            factory("lambda", spec["monitor_region"])
            .get_function_concurrency(FunctionName=arn.rsplit(":", 1)[0])
            .get("ReservedConcurrentExecutions")
        )
        if reserved != properties.get("ReservedConcurrentExecutions"):
            raise VerificationError("Durable function reserved capacity differs from the plan")
    return {"status": "PASS", "functions": len(output)}


def seal_runtime(bundle, stage="durable-runtime", *, factory=clients):
    require_reviewed_source(bundle)
    spec = bundle["spec"]
    assert_account(factory("sts", spec["monitor_region"]), spec)
    if stage not in IMMUTABLE:
        raise VerificationError("Only immutable release stages can be sealed")
    cfn, stack = owned_stack(spec, stage, factory=factory)
    if stack["StackStatus"] != "CREATE_COMPLETE":
        raise VerificationError("Only a newly created runtime can be sealed")
    target = name(spec, stage, True)
    cfn.set_stack_policy(StackName=target, StackPolicyBody=json.dumps(release.SEALED_POLICY))
    cfn.update_termination_protection(StackName=target, EnableTerminationProtection=True)
    if (
        not cfn.describe_stacks(StackName=target)["Stacks"][0].get("EnableTerminationProtection")
        or json.loads(cfn.get_stack_policy(StackName=target)["StackPolicyBody"]) != release.SEALED_POLICY
    ):
        raise VerificationError("Durable runtime protection did not take effect")
    return {"status": "SEALED", "stack": target}


def promotion_gate(bundle, receipt):
    owned_ops.promotion_gate(bundle, receipt, clients)


def verify_capture(bundle):
    spec = bundle["spec"]
    owned_stack(spec, "durable-foundation")
    outputs = collect(bundle, "durable-foundation")
    if outputs != bundle["bindings"]["foundation"]:
        raise VerificationError("Durable capture outputs changed")
    topic = f"arn:aws:sns:{spec['monitor_region']}:{spec['account_id']}:{name(spec, 'alarms')}"
    subs = [
        entry
        for page in clients("sns", spec["monitor_region"])
        .get_paginator("list_subscriptions_by_topic")
        .paginate(TopicArn=topic)
        for entry in page["Subscriptions"]
    ]
    matching = [
        s
        for s in subs
        if s["Protocol"] == "sqs"
        and s["Endpoint"] == outputs["IngressQueueArn"]
        and s["SubscriptionArn"].startswith("arn:")
    ]
    if len(matching) != 1:
        raise VerificationError("Durable ingress subscription is not registered")
    attributes = clients("sns", spec["monitor_region"]).get_subscription_attributes(
        SubscriptionArn=matching[0]["SubscriptionArn"]
    )["Attributes"]
    if attributes.get("RawMessageDelivery") != "false" or json.loads(
        attributes.get("RedrivePolicy", "{}")
    ) != {"deadLetterTargetArn": outputs["DeliveryDeadArn"]}:
        raise VerificationError("Durable ingress envelope or delivery redrive policy drifted")
    return outputs


def retirement_plan(bundle, receipt):
    promotion_gate(bundle, receipt)
    outputs = verify_capture(bundle)
    verify_runtime(bundle)
    return reconcile.plan(
        bundle["spec"],
        reconcile.owned_resources(bundle["spec"], clients),
        outputs["IngressQueueArn"],
        {item["alarm_name"] for item in alarm_descriptors(bundle["spec"])},
    )


def retire(bundle, receipt, reviewed):
    require_reviewed_source(bundle)
    current = retirement_plan(bundle, receipt)
    if current != reviewed:
        raise VerificationError("Retirement plan changed before durable cutover")
    return reconcile.apply(
        bundle["spec"],
        clients,
        reviewed,
        bundle["bindings"]["foundation"]["IngressQueueArn"],
        {item["alarm_name"] for item in alarm_descriptors(bundle["spec"])},
    )


def verify_ui_role(bundle, factory):
    """Check the actual post-promotion workload role, including trust and extra grants."""
    spec = bundle["spec"]
    _, stack = owned_stack(spec, "routing", factory=factory)
    outputs = {i["OutputKey"]: i["OutputValue"] for i in stack.get("Outputs", [])}
    arn = outputs.get("UiRoleArn", "")
    if not re.fullmatch(
        rf"arn:aws:iam::{spec['account_id']}:role/{re.escape(spec['project'])}/{spec['environment']}/[A-Za-z0-9+=,.@_-]+",
        arn,
    ):
        raise VerificationError("UI workload role ownership drifted")
    planned = durable_templates.active_routing(
        spec,
        bundle["bindings"]["foundation"],
        bundle["bindings"]["versions"],
        bundle["config"]["investigation_paused"],
        config=bundle["config"],
        owned_bindings=bundle["bindings"],
    )["Resources"]["UiRole"]["Properties"]
    client = factory("iam", spec["monitor_region"])
    if client.get_role(RoleName=arn.rsplit("/", 1)[-1])["Role"].get("Arn") != arn:
        raise VerificationError("UI workload role ARN differs from routing output")
    owned_ops.verify_role(client, arn, planned)


def verify_routing(bundle):
    spec = bundle["spec"]
    assert_account(clients("sts", spec["monitor_region"]), spec)
    owned_stack(spec, "routing")
    verify_ui_role(bundle, clients)
    outputs = verify_capture(bundle)
    versions = bundle["bindings"]["versions"]
    desired = durable_templates.active_routing(
        spec,
        outputs,
        versions,
        bundle["config"]["investigation_paused"],
        config=bundle["config"],
        owned_bindings=bundle["bindings"],
    )["Resources"]
    sources = {
        "Ingress": outputs["IngressQueueArn"],
        "Dispatch": outputs["StreamArn"],
        "Investigate": outputs["WorkQueueArn"],
        "Initial": outputs["InitialQueueArn"],
        "Report": outputs["ReportQueueArn"],
    }
    client = clients("lambda", spec["monitor_region"])
    for logical, source in sources.items():
        arn = versions[logical + "VersionArn"]
        mappings = [
            entry
            for page in client.get_paginator("list_event_source_mappings").paginate(FunctionName=arn)
            for entry in page["EventSourceMappings"]
        ]
        paused = spec["maintenance_mode"] or (
            logical == "Investigate" and bundle["config"]["investigation_paused"]
        )
        expected_state = "Disabled" if paused else "Enabled"
        if (
            len(mappings) != 1
            or mappings[0]["EventSourceArn"] != source
            or mappings[0]["State"] != expected_state
        ):
            raise VerificationError("Durable event mapping is missing, duplicated or disabled")
        properties = desired[
            "StreamMapping"
            if logical == "Dispatch"
            else ("WorkMapping" if logical == "Investigate" else logical + "Mapping")
        ]["Properties"]
        if any(mappings[0].get(key) != properties[key] for key in ("BatchSize", "FunctionResponseTypes")):
            raise VerificationError("Durable mapping batch size or partial failure handling drifted")
        if logical == "Investigate" and mappings[0].get("ScalingConfig") != {"MaximumConcurrency": 2}:
            raise VerificationError("Investigation concurrency cap drifted")
    events = clients("events", spec["monitor_region"])
    sweep = desired["SweepRule"]["Properties"]
    rule = events.describe_rule(Name=sweep["Name"])
    targets = events.list_targets_by_rule(Rule=sweep["Name"])
    if (
        rule["State"] != sweep["State"]
        or rule["ScheduleExpression"] != sweep["ScheduleExpression"]
        or targets.get("NextToken")
        or targets["Targets"] != sweep["Targets"]
    ):
        raise VerificationError("Incident reconciler schedule or target drifted")
    # Last: an unconfirmed email is reported as a wait only when every other check above has passed.
    routing_health(
        spec,
        clients,
        outputs["IngressQueueArn"],
        ec2_targets=desired["Ec2Down"]["Properties"]["Targets"],
    )
    return {"status": "PASS", "scope": "registration only; live delivery and latency still need tests"}


def execute(bundle, directory, stage, change_set_id, change_hash, receipt=None, retirement=None):
    require_reviewed_source(bundle)
    spec = bundle["spec"]
    assert_account(clients("sts", spec["monitor_region"]), spec)
    if bundle["source_dirty"]:
        raise VerificationError("Commit reviewed source before execution")
    if stage == "observations":
        from infra.observations import verify_runtime as verify_observers

        verify_observers({**bundle, "directory": str(directory)}, clients)
    if stage == "routing":
        if retirement is None:
            raise VerificationError("Promotion requires prior release, canary receipt and retirement diff")
        runtime = {**bundle, "directory": str(directory)}
        actual = retirement_plan(runtime, receipt)
        if actual != retirement or actual["disable_alarms"] or actual["unsubscribe"]:
            raise VerificationError("Retire obsolete direct routing and review the resulting empty diff")
    response, actual_hash = release.inspect_change_set(bundle, stage, change_set_id, clients)
    if actual_hash != change_hash:
        raise VerificationError("Durable change set differs from the reviewed diff")
    clients("cloudformation", bundle["stages"][stage]["region"]).execute_change_set(
        StackName=bundle["stages"][stage]["stack"],
        ChangeSetName=response["ChangeSetId"],
        ClientRequestToken=change_hash,
    )
    return {
        "status": "EXECUTION_REQUESTED",
        "stage": stage,
        "next": "Wait for CREATE_COMPLETE or UPDATE_COMPLETE and verify live routing",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "upload",
            "collect",
            "change-set",
            "inspect",
            "execute",
            "seal-runtime",
            "verify-runtime",
            "retirement-plan",
            "retire",
            "verify-routing",
            "verify-candidate",
            "canary",
            "cursor-version",
            "model-secret-version",
            "verify-observations",
            "verify-observation-routing",
            "attest-email",
            "seed-health",
        ),
    )
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--review-hash", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--stage", choices=sorted(STAGES))
    parser.add_argument(
        "--artifact-kind", choices=("pipeline", "tools", "host", "observation"), default="pipeline"
    )
    parser.add_argument("--allow-model-invocation", action="store_true")
    parser.add_argument("--confirm-inbox-delivery", action="store_true")
    parser.add_argument("--notification-id")
    parser.add_argument("--change-set")
    parser.add_argument("--change-set-hash")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--retirement-plan", type=Path)
    args = parser.parse_args()
    try:
        bundle = read_bundle(args.bundle, args.review_hash)
        if bundle["spec"]["reference_only"]:
            raise VerificationError("Synthetic reference inputs cannot be used for cloud operations")
        if args.command == "upload":
            value = upload(bundle, args.build_dir, args.artifact_kind)
        elif args.command == "seed-health":
            from infra.observations import seed_health
            from tracaus.runtime import sdk_client

            value = seed_health(
                bundle,
                lambda service, region: (
                    sdk_client(service, region, 185) if service == "lambda" else clients(service, region)
                ),
            )
        elif args.command == "verify-observations":
            from infra.observations import verify_runtime

            value = verify_runtime(bundle, clients)
        elif args.command == "verify-observation-routing":
            from infra.observations import verify_registration

            value = verify_registration(bundle, clients)
        elif args.command == "attest-email":
            require_reviewed_source(bundle)
            from infra.observations import attest_email

            value = attest_email(bundle, clients, args.notification_id, confirm=args.confirm_inbox_delivery)
        elif args.command == "verify-candidate":
            value = owned_ops.verify_candidate({**bundle, "directory": str(args.bundle)}, clients)
        elif args.command == "canary":
            value = owned_ops.canary(
                {**bundle, "directory": str(args.bundle)},
                clients,
                allow_model_invocation=args.allow_model_invocation,
            )
        elif args.command == "cursor-version":
            spec = bundle["spec"]
            assert_account(clients("sts", spec["bedrock_region"]), spec)
            secret = clients("secretsmanager", spec["bedrock_region"]).describe_secret(
                SecretId=name(spec, "")[:-1] + "/log-cursor"
            )
            versions = [v for v, stages in secret["VersionIdsToStages"].items() if "AWSCURRENT" in stages]
            if len(versions) != 1:
                raise VerificationError("Cursor secret has no unique current version")
            value = {"arn": secret["ARN"], "version_id": versions[0]}
        elif args.command == "model-secret-version":
            # Out-of-band secret: pin its single AWSCURRENT version; the value is never read.
            spec = bundle["spec"]
            if spec.get("model_provider", "bedrock") != "model_api":
                raise VerificationError("Model API key secret applies only to model_provider model_api")
            assert_account(clients("sts", spec["bedrock_region"]), spec)
            secret_name = prefix(spec) + "/model-api-key"
            secret = clients("secretsmanager", spec["bedrock_region"]).describe_secret(SecretId=secret_name)
            versions = [
                v for v, stages in secret.get("VersionIdsToStages", {}).items() if "AWSCURRENT" in stages
            ]
            if secret.get("Name") != secret_name or len(versions) != 1:
                raise VerificationError("Model API key secret has no unique current version")
            value = {"arn": secret["ARN"], "version_id": versions[0]}
        elif args.command == "collect":
            value = collect(bundle, args.stage)
        elif args.command == "change-set":
            value = change_set(bundle, args.bundle, args.stage)
        elif args.command == "inspect":
            response, checked = release.inspect_change_set(bundle, args.stage, args.change_set, clients)
            value = {
                "review_hash": checked,
                "changes": response["Changes"],
                "change_set": response["ChangeSetId"],
            }
        elif args.command == "verify-runtime":
            value = verify_runtime({**bundle, "directory": str(args.bundle)})
        elif args.command == "seal-runtime":
            value = seal_runtime(bundle, args.stage or "durable-runtime")
        elif args.command == "verify-routing":
            value = verify_routing(bundle)
        elif args.command in {"retirement-plan", "retire"}:
            receipt = json.loads(args.receipt.read_text()) if args.receipt else None
            current = {**bundle, "directory": str(args.bundle)}
            value = (
                retirement_plan(current, receipt)
                if args.command == "retirement-plan"
                else retire(current, receipt, json.loads(args.retirement_plan.read_text()))
            )
        else:
            value = execute(
                bundle,
                args.bundle,
                args.stage,
                args.change_set,
                args.change_set_hash,
                json.loads(args.receipt.read_text()) if args.receipt else None,
                json.loads(args.retirement_plan.read_text()) if args.retirement_plan else None,
            )
        release.write_json(args.output, value)
        print(f"Saved {args.command} result to {args.output}")
        return 0
    except PendingConfirmation as exc:
        # Distinct from failure: the automation waits for the human click and then repeats this check.
        release.write_json(args.output, {"status": "PENDING_CONFIRMATION", "recipients": exc.recipients})
        print("Email subscriptions await confirmation; no success receipt was produced.", file=sys.stderr)
        return PendingConfirmation.EXIT_CODE
    except (VerificationError, ValueError, KeyError, OSError, TypeError, BotoCoreError, ClientError):
        print("Durable operation failed; no success receipt was produced.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
