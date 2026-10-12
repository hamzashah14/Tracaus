"""Lint complete durable pipeline templates in same-region and split-region synthetic layouts."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from infra.spec import TOOL_LOGICAL, load, name, prefix, tool_functions  # noqa: E402
from infra.stacks import durable_templates, owned_runtime, templates  # noqa: E402


def examples(spec, config, *, include_bindings=False):
    region, account = spec["monitor_region"], spec["account_id"]
    foundation = {
        "TableArn": f"arn:aws:dynamodb:{region}:{account}:table/{name(spec, 'incidents')}",
        "TableName": name(spec, "incidents"),
        "StreamArn": f"arn:aws:dynamodb:{region}:{account}:table/{name(spec, 'incidents')}/stream/2026-10-05T00:00:00.000",
        "EvidenceBucket": durable_templates.report_bucket(spec),
        "EvidenceKeyArn": f"arn:aws:kms:{region}:{account}:key/12345678-1234-1234-1234-123456789012",
        "StreamDeadArn": f"arn:aws:sqs:{region}:{account}:{name(spec, 'stream-dead')}",
        "DeliveryDeadArn": f"arn:aws:sqs:{region}:{account}:{name(spec, 'delivery-dead')}",
    }
    for key in durable_templates.QUEUES:
        foundation[key + "QueueArn"] = durable_templates.queue_arn(spec, key)
        foundation[key + "QueueUrl"] = (
            f"https://sqs.{region}.amazonaws.com/{account}/{durable_templates.queue_name(spec, key)}"
        )
    artifacts = {}
    versions = {}
    for function in durable_templates.FUNCTIONS:
        artifacts[function] = {
            "bucket": templates.bucket_name(spec, "monitor"),
            "key": f"releases/{spec['release_id']}/" + "a" * 64 + ".zip",
            "version_id": "synthetic-object-version",
            "sha256": "a" * 64,
        }
        logical = function.removeprefix("incident_").title()
        versions[logical + "VersionArn"] = (
            f"arn:aws:lambda:{region}:{account}:function:{name(spec, function.replace('_', '-'), True)}:1"
        )
    tools = {
        TOOL_LOGICAL[function]
        + "VersionArn": f"arn:aws:lambda:{spec['bedrock_region']}:{account}:function:{name(spec, function.replace('_', '-'), True)}:1"
        for function in tool_functions(spec)
    }
    bindings = {"tools": tools, "foundation": foundation}
    if owned_runtime.model_api(spec):
        bindings["model_secret"] = {
            "arn": f"arn:aws:secretsmanager:{spec['bedrock_region']}:{account}:secret:{prefix(spec)}/model-api-key-AbCdEf",
            "version_id": "a" * 32,
        }
    tool_artifacts = {
        n: {**artifacts["incident_ingress"], "bucket": templates.bucket_name(spec, "tools")}
        for n in tool_functions(spec)
    }
    result = {
        "durable-foundation": durable_templates.foundation(spec, config),
        "owned-tools": templates.tools_release(
            spec,
            tool_artifacts,
            {
                "arn": f"arn:aws:secretsmanager:{spec['bedrock_region']}:{account}:secret:tracaus/staging/cursor-123456",
                "version_id": "a" * 32,
            },
        ),
    }
    if config["runtime_target"] == "agentcore":
        runtime_id = name(spec, "agentcore", True).replace("-", "_") + "-1234567890"
        arn = f"arn:aws:bedrock-agentcore:{spec['bedrock_region']}:{account}:runtime/{runtime_id}"
        endpoint = "release_" + spec["release_id"].replace("-", "_")
        bindings["agentcore"] = {
            "RuntimeArn": arn,
            "EndpointName": endpoint,
            "RuntimeVersion": "1",
            "EndpointArn": arn + "/runtime-endpoint/" + endpoint + "-1234567890",
        }
        result["agentcore-runtime"] = owned_runtime.agentcore_release(
            spec, config, bindings, tool_artifacts["fetch_logs"]
        )
        result["agentcore-endpoint"] = owned_runtime.agentcore_endpoint(spec, runtime_id, "1")
    result["durable-runtime"] = durable_templates.runtime(
        spec, config, artifacts, foundation, owned_bindings=bindings
    )
    result["routing"] = durable_templates.active_routing(
        spec,
        foundation,
        versions,
        config["investigation_paused"],
        config=config,
        owned_bindings=bindings,
    )
    if "bedrock:InvokeAgent" in json.dumps(result) or "AWS::Bedrock::Agent" in json.dumps(result):
        raise AssertionError("Unexpected agent resource in the owned runtime plan")
    return (result, bindings, artifacts, versions) if include_bindings else result


def main():
    config = json.loads((ROOT / "examples/durable.example.json").read_text())
    with tempfile.TemporaryDirectory(prefix="tracaus-durable-") as temp:
        files = []
        for example in ("deployment", "ecs"):  # EC2 instances, then ECS services
            spec = load(ROOT / f"examples/{example}.example.json")
            for mode in ("same", "split"):
                if mode == "split":
                    spec = {**spec, "bedrock_region": "us-east-1"}
                for target in ("standalone", "agentcore"):
                    for stage, value in examples(spec, {**config, "runtime_target": target}).items():
                        path = Path(temp) / f"{example}-{mode}-{target}-{stage}.json"
                        path.write_text(json.dumps(value))
                        files.append(str(path))
                api = {
                    **{k: v for k, v in spec.items() if k != "model_arns"},
                    "model_provider": "model_api",
                    "model_id": "provider-model",
                    "model_api": {"protocol": "openai", "base_url": "https://api.example.com/v1"},
                }
                for stage, value in examples(api, {**config, "runtime_target": "standalone"}).items():
                    path = Path(temp) / f"{example}-{mode}-model-api-{stage}.json"
                    path.write_text(json.dumps(value))
                    files.append(str(path))
        subprocess.run(
            [str(Path(sys.executable).parent / "cfn-lint"), "--non-zero-exit-code", "warning", "-t", *files],
            check=True,
        )
    print(f"PASS: {len(files)} standalone/AgentCore/model-API same/split-region templates")


if __name__ == "__main__":
    main()
