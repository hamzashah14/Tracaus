"""Verify owned releases and produce a paid staging canary receipt, fail closed."""

import json
import uuid
from datetime import datetime, timezone

from infra import owned_runtime, release
from infra.spec import TOOL_LOGICAL, canary_target, canary_tools, tool_functions
from infra.verify import VerificationError, coverage, verify_function
from tracaus.runtime import sdk_client


def verify_role(client, arn, planned):
    name = arn.rsplit("/", 1)[-1]
    actual = client.get_role(RoleName=name)["Role"]
    if actual.get("PermissionsBoundary") or actual.get("Path") != planned["Path"]:
        raise VerificationError("Execution role path or permissions boundary differs from the release")
    if actual["AssumeRolePolicyDocument"] != planned["AssumeRolePolicyDocument"]:
        raise VerificationError("Execution role trust differs from the release")
    inline = client.list_role_policies(RoleName=name)
    attached = client.list_attached_role_policies(RoleName=name)
    if (
        inline.get("IsTruncated")
        or attached.get("IsTruncated")
        or attached.get("AttachedPolicies")
        or set(inline["PolicyNames"]) != {"runtime"}
    ):
        raise VerificationError("Unexpected execution role policies")
    actual_policy = client.get_role_policy(RoleName=name, PolicyName="runtime")["PolicyDocument"]
    if actual_policy != planned["Policies"][0]["PolicyDocument"]:
        raise VerificationError("Execution role grants differ from the release")


def sealed(bundle, stage, factory):
    info = bundle["stages"][stage]
    client = factory("cloudformation", info["region"])
    stack = client.describe_stacks(StackName=info["stack"])["Stacks"][0]
    if (
        stack["StackStatus"] != "CREATE_COMPLETE"
        or not stack.get("EnableTerminationProtection")
        or json.loads(client.get_stack_policy(StackName=info["stack"])["StackPolicyBody"])
        != release.SEALED_POLICY
    ):
        raise VerificationError("Seal every immutable candidate stage before promotion")
    body = client.get_template(StackName=info["stack"])["TemplateBody"]
    if isinstance(body, str):
        body = json.loads(body)
    from infra.templates import template_hash

    if template_hash(body) != info["template_hash"]:
        raise VerificationError("Sealed candidate template drifted")
    return body


def verify_agentcore_logs(spec, remote, factory):
    client = factory("logs", spec["bedrock_region"])
    runtime_id = remote["RuntimeArn"].split("/")[-1]
    for endpoint in (remote["EndpointName"], "DEFAULT"):
        group = owned_runtime.agentcore_log_group(runtime_id, endpoint)
        response = client.describe_log_groups(logGroupNamePrefix=group)
        matches = [r for r in response.get("logGroups", []) if r["logGroupName"] == group]
        if (
            response.get("nextToken")
            or len(matches) != 1
            or (matches[0].get("retentionInDays") != spec["log_retention_days"])
        ):
            raise VerificationError("AgentCore application log group or retention drifted")


def verify_candidate(bundle, factory):
    spec, bindings, config = bundle["spec"], bundle["bindings"], bundle["config"]
    from infra.verify import assert_account

    assert_account(factory("sts", spec["monitor_region"]), spec)
    owned_runtime.validate_bindings(spec, config, bindings)
    planned = sealed(bundle, "owned-tools", factory)
    for function in tool_functions(spec):
        logical = TOOL_LOGICAL[function]
        arn = bindings["tools"][logical + "VersionArn"]
        client = factory("lambda", spec["bedrock_region"])
        got = verify_function(client, arn, bindings["tool_artifacts"][function])
        properties = planned["Resources"][logical]["Properties"]
        actual = client.get_function_configuration(FunctionName=arn)
        if got["configuration"] != properties["Environment"]["Variables"] or any(
            actual.get(key) != properties[key] for key in ("Timeout", "MemorySize", "Architectures")
        ):
            raise VerificationError("Tool capacity/configuration drifted")
        verify_role(
            factory("iam", spec["bedrock_region"]),
            actual["Role"],
            planned["Resources"][logical + "Role"]["Properties"],
        )
    if config["runtime_target"] == "agentcore":
        verify_remote(bundle, factory)

    # Verify the six queue consumers, especially the actual caller's IAM role.
    from infra.durable_ops import verify_runtime

    verify_runtime(bundle, factory=factory)
    return {
        "status": "PASS",
        "runtime_target": config["runtime_target"],
        "scope": "release bindings; model and tool capability require canary",
    }


def verify_remote(bundle, factory):
    spec, bindings = bundle["spec"], bundle["bindings"]
    remote = bindings["agentcore"]
    planned = sealed(bundle, "agentcore-runtime", factory)
    sealed(bundle, "agentcore-endpoint", factory)
    verify_agentcore_logs(spec, remote, factory)
    control = factory("bedrock-agentcore-control", spec["bedrock_region"])
    runtime_id = remote["RuntimeArn"].split("/")[-1]
    actual = control.get_agent_runtime(
        agentRuntimeId=runtime_id, agentRuntimeVersion=remote["RuntimeVersion"]
    )
    expected = planned["Resources"]["Runtime"]["Properties"]
    artifact = bindings["host_artifact"]
    wanted_artifact = {
        "codeConfiguration": {
            "code": {
                "s3": {
                    "bucket": artifact["bucket"],
                    "prefix": artifact["key"],
                    "versionId": artifact["version_id"],
                }
            },
            "runtime": "PYTHON_3_12",
            "entryPoint": ["tracaus_agentcore.py"],
        }
    }
    if (
        actual.get("status") != "READY"
        or actual.get("agentRuntimeArn") != remote["RuntimeArn"]
        or actual.get("agentRuntimeVersion") != remote["RuntimeVersion"]
        or actual.get("agentRuntimeArtifact") != wanted_artifact
        or actual.get("environmentVariables") != expected["EnvironmentVariables"]
        or actual.get("networkConfiguration") != {"networkMode": "PUBLIC"}
        or actual.get("protocolConfiguration") != "HTTP"
        or actual.get("lifecycleConfiguration") != {"idleRuntimeSessionTimeout": 60, "maxLifetime": 600}
        or actual.get("authorizerConfiguration")
    ):
        raise VerificationError("AgentCore runtime artifact/configuration/authentication drifted")
    endpoint = control.get_agent_runtime_endpoint(
        agentRuntimeId=runtime_id, endpointName=remote["EndpointName"]
    )
    if (
        endpoint.get("status") != "READY"
        or endpoint.get("liveVersion") != remote["RuntimeVersion"]
        or endpoint.get("targetVersion") != remote["RuntimeVersion"]
        or endpoint.get("agentRuntimeEndpointArn") != remote["EndpointArn"]
    ):
        raise VerificationError("AgentCore endpoint version drifted")
    verify_role(
        factory("iam", spec["bedrock_region"]),
        actual["roleArn"],
        planned["Resources"]["ExecutionRole"]["Properties"],
    )
    s3 = factory("s3", spec["bedrock_region"])
    import hashlib

    body = s3.get_object(
        Bucket=artifact["bucket"],
        Key=artifact["key"],
        VersionId=artifact["version_id"],
        ExpectedBucketOwner=spec["account_id"],
    )["Body"]
    try:
        data = body.read(40 * 1024 * 1024 + 1)
    finally:
        body.close()
    if len(data) > 40 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != artifact["sha256"]:
        raise VerificationError("AgentCore host artifact differs from its reviewed checksum")


def canary(bundle, factory, *, allow_model_invocation=False, client=None):
    if not allow_model_invocation or bundle["spec"]["environment"] != "staging":
        raise VerificationError("Canary requires explicit paid invocation authorization in staging")
    verify_candidate(bundle, factory)
    coverage(bundle["spec"], factory)
    release_hash = owned_runtime.fingerprint(bundle["spec"], bundle["config"], bundle["bindings"])
    iid = canary_target(bundle["spec"])
    window = bundle["config"]["runtime_limits"]["window_minutes"]
    payload = {
        "version": 1,
        "release": release_hash,
        "mode": "chat",
        "history": [],
        "prompt": (
            f"Investigate ECS service {iid} using fetch_ecs and fetch_metrics (and fetch_logs discovery/search if it has log groups). State missing data and uncertainty; use a window of at most {window} minutes per side."
            if iid.startswith("ecs-")
            else f"Investigate instance {iid} using fetch_logs discovery/search and fetch_metrics. State missing data and uncertainty; use a window of at most {window} minutes per side."
        ),
    }
    client = client or sdk_client("lambda", bundle["spec"]["monitor_region"], 210)
    response = client.invoke(
        FunctionName=bundle["bindings"]["versions"]["InvestigateVersionArn"],
        InvocationType="RequestResponse",
        Payload=json.dumps({"runtime_canary": payload}).encode(),
    )
    stream = response["Payload"]
    try:
        data = stream.read(96001)
    finally:
        stream.close()
    if len(data) > 96000 or response.get("FunctionError") or response.get("StatusCode") != 200:
        raise VerificationError("Owned runtime canary failed")
    result = json.loads(data)
    if (
        result.get("release") != release_hash
        or result.get("complete") is not True
        or not canary_tools(bundle["spec"]) <= set(result.get("tools", []))
        or not canary_tools(bundle["spec"]) <= set(result.get("evidence", []))
        or not result.get("text")
        or result.get("diagnosis", {}).get("status") != "VALID"
        or result.get("diagnosis", {}).get("policy") != "diagnosis-v1"
    ):
        raise VerificationError("Canary did not prove both successful tool contracts and model completion")
    return {
        "status": "PASS",
        "bundle_hash": bundle["review_hash"],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "runtime_release": release_hash,
        "runtime_target": bundle["config"]["runtime_target"],
        "canary_tools": result["tools"],
        "usage": result["usage"],
        "request_id": str(uuid.uuid4()),
    }


def promotion_gate(bundle, receipt, factory):
    release.require_receipt(bundle, receipt)
    if (
        receipt.get("runtime_release")
        != owned_runtime.fingerprint(bundle["spec"], bundle["config"], bundle["bindings"])
        or receipt.get("runtime_target") != bundle["config"]["runtime_target"]
    ):
        raise VerificationError("Canary target/model/tool/limit binding differs from the promotion")
    verify_candidate(bundle, factory)
    coverage(bundle["spec"], factory)
