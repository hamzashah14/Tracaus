"""Validate complete observation additions in both hosting and region layouts, without AWS."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from infra.spec import load, name  # noqa: E402
from infra.stacks import observation_templates  # noqa: E402
from scripts.dev.validate_durable import examples, lint  # noqa: E402


def fixtures(spec, config):
    prior, bindings, artifacts, versions = examples(spec, config, include_bindings=True)
    observation_versions = {
        logical
        + "VersionArn": f"arn:aws:lambda:{spec['monitor_region']}:{spec['account_id']}:function:{name(spec, function.replace('_', '-'), True)}:1"
        for function, logical in observation_templates.FUNCTIONS.items()
    }
    observation_artifacts = {
        function: artifacts["incident_ingress"] for function in observation_templates.FUNCTIONS
    }
    return {
        **prior,
        "observation-foundation": observation_templates.foundation(spec, bindings["foundation"], config),
        "observation-runtime": observation_templates.runtime(
            spec, bindings["foundation"], observation_artifacts, config
        ),
        "observations": observation_templates.active(
            spec,
            bindings["foundation"],
            observation_versions,
            versions,
            runtime_target=config["runtime_target"],
            agentcore=bindings.get("agentcore"),
        ),
    }


def main():
    spec = load(ROOT / "examples/observability.example.json")
    config = json.loads((ROOT / "examples/durable.example.json").read_text())
    rendered = {}
    for mode in ("same", "split"):
        current = {**spec, "bedrock_region": "us-east-1" if mode == "split" else spec["bedrock_region"]}
        for target in ("standalone", "agentcore"):
            for stage, value in fixtures(current, {**config, "runtime_target": target}).items():
                rendered[f"{mode}-{target}-{stage}.json"] = value
    print(f"PASS: {lint(rendered)} observation same/split-region standalone/AgentCore templates")


if __name__ == "__main__":
    main()
