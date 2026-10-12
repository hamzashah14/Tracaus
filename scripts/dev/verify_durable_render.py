"""Exercise complete owned release rendering with real local builds and synthetic bindings.

Never constructs an AWS client; reference_only remains true in every output.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from infra.deploy import durable, durable_ops  # noqa: E402
from infra.spec import load, name, prefix  # noqa: E402
from infra.stacks import templates  # noqa: E402
from scripts.dev.validate_durable import examples  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool-build-dir", type=Path, default=ROOT / ".build/reference-lambda")
    parser.add_argument("--observations", action="store_true")
    args = parser.parse_args()
    spec_path = ROOT / (
        "examples/observability.example.json" if args.observations else "examples/deployment.example.json"
    )
    spec = load(spec_path)
    base = json.loads((ROOT / "examples/durable.example.json").read_text())
    pipeline, host = ROOT / ".build/pipeline-a", ROOT / ".build/agentcore-a"

    def pin(build, purpose, function):
        sha = json.loads((build / "manifest.json").read_text())["functions"][function]["sha256"]
        return {
            "bucket": templates.bucket_name(spec, purpose),
            "key": f"releases/{spec['release_id']}/{sha}.zip",
            "sha256": sha,
            "version_id": "synthetic-version",
        }

    evidence = {}
    for target, count in (("standalone", 6), ("agentcore", 8)):
        config = {**base, "runtime_target": target}
        _, bindings, artifacts, versions = examples(spec, config, include_bindings=True)
        bindings.update(
            {
                "artifacts": {n: pin(pipeline, "monitor", n) for n in artifacts},
                "versions": versions,
                "tool_artifacts": {
                    n: pin(args.tool_build_dir, "tools", n) for n in ("fetch_logs", "fetch_metrics")
                },
                "secret": {
                    "arn": f"arn:aws:secretsmanager:{spec['bedrock_region']}:{spec['account_id']}:secret:{prefix(spec)}/log-cursor-123456",
                    "version_id": "a" * 32,
                },
            }
        )
        if target == "agentcore":
            bindings["host_artifact"] = pin(host, "tools", "incident_investigate")
            bindings["agentcore_candidate"] = {
                "RuntimeId": bindings["agentcore"]["RuntimeArn"].split("/")[-1],
                "RuntimeVersion": "1",
            }
        if args.observations:
            from infra.stacks.observation_templates import FUNCTIONS

            observation = ROOT / ".build/observation-a"
            bindings["observation_artifacts"] = {n: pin(observation, "monitor", n) for n in FUNCTIONS}
            bindings["observation_versions"] = {
                logical
                + "VersionArn": f"arn:aws:lambda:{spec['monitor_region']}:{spec['account_id']}:function:{name(spec, n.replace('_', '-'), True)}:1"
                for n, logical in FUNCTIONS.items()
            }
            count += 3
        kind = "observation" if args.observations else "durable"
        folder = ROOT / f".build/{kind}-render" / target
        folder.mkdir(parents=True, exist_ok=True)
        config_path, bindings_path = folder / "config.json", folder / "bindings.json"
        config_path.write_text(json.dumps(config))
        bindings_path.write_text(json.dumps(bindings))
        bundle = durable.render(
            spec_path,
            config_path,
            folder / "plan",
            pipeline,
            bindings_path,
            args.tool_build_dir,
            host if target == "agentcore" else None,
            ROOT / ".build/observation-a" if args.observations else None,
        )
        checked = durable_ops.read_bundle(folder / "plan", bundle["review_hash"])
        assert checked == bundle and len(checked["stages"]) == count and checked["spec"]["reference_only"]
        evidence[target] = {"stages": count, "status": "PASS", "synthetic_only": True}
    (ROOT / f".build/{kind}-render-validation.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print("PASS: complete release renders and bundle verification", evidence)


if __name__ == "__main__":
    main()
