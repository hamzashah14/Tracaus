"""Owned orchestration release bindings and target-specific least-privilege templates."""

import hashlib
import json
import re
from dataclasses import asdict

from infra.deploy.verify import VerificationError
from infra.spec import (
    ROOT,
    TOOL_LOGICAL,
    digest,
    ecs_services,
    name,
    prefix,
    tags,
    targets,
    tool_functions,
)
from infra.stacks.templates import att, resource, role, statement, tagged, template
from tracaus.agentcore import validate_target
from tracaus.runtime import Limits


def model_api(spec):
    """The generic HTTPS model API settings, or None when Bedrock is the provider."""
    return spec["model_api"] if spec.get("model_provider", "bedrock") == "model_api" else None


def model_secret(spec, bindings):
    """Exact owned model API key secret and immutable version; created out of band."""
    try:
        value = bindings["model_secret"]
        if (
            set(value) != {"arn", "version_id"}
            or not re.fullmatch(
                rf"arn:aws:secretsmanager:{re.escape(spec['bedrock_region'])}:{spec['account_id']}:secret:{re.escape(prefix(spec))}/model-api-key-[A-Za-z0-9]{{6}}",
                value["arn"],
            )
            or not re.fullmatch(r"[A-Za-z0-9-]{32,64}", value["version_id"])
        ):
            raise ValueError()
    except Exception:
        raise VerificationError(
            "Bind the exact owned model API key secret ARN and immutable version"
        ) from None
    return value


def validate_bindings(spec, config, bindings):
    if spec["executor_mode"] != "qualified":
        raise VerificationError("Owned runtime requires qualified tool executors")
    if model_api(spec):
        if config["runtime_target"] != "standalone":
            raise VerificationError(
                "model_api is supported only by the standalone runtime; AgentCore is Bedrock-only"
            )
        model_secret(spec, bindings)
    tools = bindings.get("tools", {})
    wanted = {TOOL_LOGICAL[f] + "VersionArn": f.replace("_", "-") for f in tool_functions(spec)}
    if set(tools) != set(wanted):
        raise VerificationError("Every tool version binding the deployment needs is required")
    for key, function in wanted.items():
        prefix = f"arn:aws:lambda:{spec['bedrock_region']}:{spec['account_id']}:function:{name(spec, function, True)}:"
        if not isinstance(tools[key], str) or not re.fullmatch(
            re.escape(prefix) + r"[1-9][0-9]*", tools[key]
        ):
            raise VerificationError("Tool binding is outside this qualified release")
    if "agentcore" in bindings:
        remote = bindings["agentcore"]
        if set(remote) != {"RuntimeArn", "EndpointName", "RuntimeVersion", "EndpointArn"}:
            raise VerificationError("Incomplete AgentCore release bindings")
        validate_target(
            remote["RuntimeArn"], remote["EndpointName"], spec["bedrock_region"], spec["account_id"]
        )
        expected = name(spec, "agentcore", True).replace("-", "_")
        if not remote["RuntimeArn"].split("/")[-1].startswith(expected + "-") or remote[
            "EndpointName"
        ] != "release_" + spec["release_id"].replace("-", "_"):
            raise VerificationError("AgentCore endpoint belongs to a different release")
        if not re.fullmatch(r"[1-9][0-9]{0,4}", remote["RuntimeVersion"]):
            raise VerificationError("AgentCore endpoint must bind a numeric runtime version")
        if not re.fullmatch(
            re.escape(remote["RuntimeArn"] + "/runtime-endpoint/" + remote["EndpointName"] + "-")
            + r"[A-Za-z0-9]{10}",
            remote["EndpointArn"],
        ):
            raise VerificationError("AgentCore endpoint ARN mismatch")


def fingerprint(spec, config, bindings):
    return digest(
        {
            "model_id": spec["model_id"],
            **(
                {"model_api": model_api(spec), "model_secret": model_secret(spec, bindings)}
                if model_api(spec)
                else {"model_arns": spec["model_arns"]}
            ),
            "tools": bindings["tools"],
            "limits": asdict(Limits(**config["runtime_limits"])),
            "runtime_target": config["runtime_target"],
            "release_id": spec["release_id"],
            "contracts": {
                p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
                for p in (
                    "agent-instruction.txt",
                    "schemas/fetch_logs.json",
                    "schemas/fetch_metrics.json",
                    *(["schemas/fetch_ecs.json"] if ecs_services(spec) else []),
                    "tracaus/diagnosis.py",
                    "tracaus/safety.py",
                )
            },
        }
    )


def model_api_environment(spec, bindings):
    api, secret = model_api(spec), model_secret(spec, bindings)
    value = {
        "protocol": api["protocol"],
        "base_url": api["base_url"],
        "secret_arn": secret["arn"],
        "secret_version": secret["version_id"],
    }
    if api["protocol"] == "openai" and "bytes_per_token" in api:
        value["bytes_per_token"] = api["bytes_per_token"]
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def environment(spec, config, bindings):
    validate_bindings(spec, config, bindings)
    env = {
        "ENVIRONMENT": spec["environment"],
        "EXECUTION_PURPOSE": "both",
        "TRACAUS_DIAGNOSTIC_POLICY": "diagnosis-v1",
        "RUNTIME_TARGET": config["runtime_target"],
        "BEDROCK_REGION": spec["bedrock_region"],
        "BEDROCK_MODEL_ID": spec["model_id"],
        "EXPECTED_ACCOUNT_ID": spec["account_id"],
        "ALLOWED_INSTANCE_IDS": ",".join(targets(spec)),
        "LOGS_TOOL_ARN": bindings["tools"]["LogsVersionArn"],
        "METRICS_TOOL_ARN": bindings["tools"]["MetricsVersionArn"],
        **(
            {"ECS_TOOL_ARN": bindings["tools"]["EcsVersionArn"]}
            if "EcsVersionArn" in bindings["tools"]
            else {}
        ),
        "RUNTIME_LIMITS": json.dumps(config["runtime_limits"], sort_keys=True, separators=(",", ":")),
        "RUNTIME_RELEASE": fingerprint(spec, config, bindings),
        **({"MODEL_API": model_api_environment(spec, bindings)} if model_api(spec) else {}),
        **(
            {"OBS_NAMESPACE": f"{spec['project']}/{spec['environment']}/Pipeline"}
            if "observability" in spec
            else {}
        ),
    }
    return env


def model_permissions(spec, bindings):
    if model_api(spec):
        secret = model_secret(spec, bindings)
        model = statement(
            "secretsmanager:GetSecretValue",
            secret["arn"],
            Condition={"StringEquals": {"secretsmanager:VersionId": secret["version_id"]}},
        )
    else:
        model = statement(["bedrock:InvokeModel", "bedrock:CountTokens"], spec["model_arns"])
    return [model, statement("lambda:InvokeFunction", list(bindings["tools"].values()))]


def caller_environment(spec, config, bindings):
    env = environment(spec, config, bindings)
    if config["runtime_target"] == "agentcore":
        remote = bindings.get("agentcore")
        if not remote:
            raise VerificationError("Collect and verify the AgentCore release before binding callers")
        env.update(
            {"AGENTCORE_RUNTIME_ARN": remote["RuntimeArn"], "AGENTCORE_ENDPOINT": remote["EndpointName"]}
        )
        for key in ("LOGS_TOOL_ARN", "METRICS_TOOL_ARN", "ECS_TOOL_ARN"):
            env.pop(key, None)  # Caller is not a tool executor.
    return env


def caller_permissions(spec, config, bindings):
    if config["runtime_target"] == "standalone":
        return model_permissions(spec, bindings)
    return [
        statement(
            "bedrock-agentcore:InvokeAgentRuntime",
            [bindings["agentcore"]["RuntimeArn"], bindings["agentcore"]["EndpointArn"]],
        )
    ]


def agentcore_release(spec, config, bindings, artifact, version=None):
    """Create-only Runtime, then a reviewed endpoint pinned to its collected version."""
    t = template(spec, spec["bedrock_region"], "Create-only code-owned AgentCore candidate")
    foundation = bindings["foundation"]
    env = {
        **environment(spec, config, bindings),
        "AGENTCORE_HOSTING": "true",
        "MONITOR_REGION": spec["monitor_region"],
        "INCIDENT_TABLE": foundation["TableName"],
        "REPORT_BUCKET": foundation["EvidenceBucket"],
        "REPORT_KMS_KEY_ARN": foundation["EvidenceKeyArn"],
    }
    runtime_name = name(spec, "agentcore", True).replace("-", "_")
    if len(runtime_name) > 48:
        raise VerificationError("AgentCore runtime name exceeds the service limit")
    t["Resources"]["ExecutionRole"] = role(
        spec,
        "bedrock-agentcore.amazonaws.com",
        [
            *model_permissions(spec, bindings),
            statement(
                ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:PutItem"],
                foundation["TableArn"],
            ),
            statement("s3:PutObject", f"arn:aws:s3:::{foundation['EvidenceBucket']}/incidents/*"),
            statement(["kms:Encrypt", "kms:GenerateDataKey"], foundation["EvidenceKeyArn"]),
            statement("s3:GetObjectVersion", f"arn:aws:s3:::{artifact['bucket']}/{artifact['key']}"),
            statement(
                "logs:CreateLogGroup",
                f"arn:aws:logs:{spec['bedrock_region']}:{spec['account_id']}:log-group:/aws/bedrock-agentcore/runtimes/{runtime_name}*",
            ),
            statement(
                ["logs:CreateLogStream", "logs:PutLogEvents"],
                f"arn:aws:logs:{spec['bedrock_region']}:{spec['account_id']}:log-group:/aws/bedrock-agentcore/runtimes/{runtime_name}*:log-stream:*",
            ),
        ],
        release=True,
        conditions={
            "StringEquals": {"aws:SourceAccount": spec["account_id"]},
            "ArnLike": {
                "aws:SourceArn": f"arn:aws:bedrock-agentcore:{spec['bedrock_region']}:{spec['account_id']}:*"
            },
        },
    )
    t["Resources"]["Runtime"] = resource(
        "BedrockAgentCore::Runtime",
        {
            "AgentRuntimeName": runtime_name,
            "RoleArn": att("ExecutionRole"),
            "AgentRuntimeArtifact": {
                "CodeConfiguration": {
                    "Code": {
                        "S3": {
                            "Bucket": artifact["bucket"],
                            "Prefix": artifact["key"],
                            "VersionId": artifact["version_id"],
                        }
                    },
                    "Runtime": "PYTHON_3_12",
                    "EntryPoint": ["tracaus_agentcore.py"],
                }
            },
            "EnvironmentVariables": env,
            "NetworkConfiguration": {"NetworkMode": "PUBLIC"},
            "ProtocolConfiguration": "HTTP",
            "LifecycleConfiguration": {"IdleRuntimeSessionTimeout": 60, "MaxLifetime": 600},
            "Tags": tags(spec, True),
        },
        retain=True,
    )
    t["Outputs"]["RuntimeArn"] = {"Value": att("Runtime", "AgentRuntimeArn")}
    t["Outputs"]["RuntimeVersion"] = {"Value": att("Runtime", "AgentRuntimeVersion")}
    t["Outputs"]["RuntimeId"] = {"Value": att("Runtime", "AgentRuntimeId")}
    if version is not None:
        raise VerificationError("Endpoint is a separate sealed stage; do not update the Runtime candidate")
    return t


def agentcore_endpoint(spec, runtime_id, version):
    if not re.fullmatch(r"[1-9][0-9]{0,4}", version):
        raise VerificationError("Endpoint requires an explicitly collected runtime version")
    t = template(spec, spec["bedrock_region"], "Create-only AgentCore endpoint pinned to a candidate version")
    endpoint = "release_" + spec["release_id"].replace("-", "_")
    # Pre-create groups before either endpoint can emit application logs. The
    # DEFAULT endpoint is not used by Tracaus, but exists for every AWS runtime.
    for logical, qualifier in (("RuntimeLogs", endpoint), ("DefaultLogs", "DEFAULT")):
        t["Resources"][logical] = resource(
            "Logs::LogGroup",
            {
                "LogGroupName": agentcore_log_group(runtime_id, qualifier),
                "RetentionInDays": spec["log_retention_days"],
                "Tags": tagged(spec),
            },
            retain=True,
        )
    t["Resources"]["Endpoint"] = resource(
        "BedrockAgentCore::RuntimeEndpoint",
        {
            "AgentRuntimeId": runtime_id,
            "AgentRuntimeVersion": version,
            "Name": endpoint,
            "Tags": tags(spec, True),
        },
        retain=True,
        depends=["RuntimeLogs", "DefaultLogs"],
    )
    t["Outputs"] = {
        "RuntimeArn": {"Value": att("Endpoint", "AgentRuntimeArn")},
        "EndpointArn": {"Value": att("Endpoint", "AgentRuntimeEndpointArn")},
        "EndpointName": {"Value": endpoint},
        "RuntimeVersion": {"Value": att("Endpoint", "LiveVersion")},
    }
    return t


def agentcore_log_group(runtime_id, endpoint):
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-{endpoint}"
