"""Lint every generated CloudFormation stage using a synthetic, non-deployable binding."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from infra.spec import load, name, tool_functions  # noqa: E402
from infra.stacks import bootstrap_iam, templates  # noqa: E402


def examples(spec):
    artifacts = {}
    for function in tool_functions(spec):
        artifacts[function] = {
            "bucket": templates.bucket_name(spec, "tools"),
            "key": f"releases/{spec['release_id']}/" + "a" * 64 + ".zip",
            "version_id": "synthetic-object-version",
            "sha256": "a" * 64,
        }
    secret = {
        "arn": f"arn:aws:secretsmanager:{spec['bedrock_region']}:{spec['account_id']}:secret:{name(spec, '')[:-1]}/log-cursor-AbCdEf",
        "version_id": "a" * 32,
    }
    return {
        "foundation-tools": templates.foundation(spec, "tools"),
        "foundation-monitor": templates.foundation(spec, "monitor"),
        "owned-tools": templates.tools_release(spec, artifacts, secret),
        "routing": templates.service_routing(spec),
    }


def main():
    with tempfile.TemporaryDirectory(prefix="tracaus-cfn-") as temp:
        files = []
        for example in ("deployment", "ecs"):  # EC2 instances, then ECS services
            spec = load(ROOT / f"examples/{example}.example.json")
            for region_mode in ("same", "split"):
                if region_mode == "split":
                    spec = {**spec, "bedrock_region": "us-east-1"}
                stages = {
                    **examples(spec),
                    "iam-bootstrap": bootstrap_iam.render(spec, agentcore=True, instance_role=True),
                }
                for stage, template in stages.items():
                    path = Path(temp) / f"{example}-{region_mode}-{stage}.json"
                    path.write_text(json.dumps(template))
                    files.append(str(path))
        subprocess.run(
            [str(Path(sys.executable).parent / "cfn-lint"), "--non-zero-exit-code", "warning", "-t", *files],
            check=True,
        )
    print(
        f"PASS: {len(files)} current foundation/tool/service-routing/iam-bootstrap templates (same and split region)"
    )


if __name__ == "__main__":
    main()
