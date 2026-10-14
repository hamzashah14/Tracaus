"""Render the durable pipeline staged resource plan from customer-owned, private bindings."""

import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path

from infra.deploy import release
from infra.deploy.verify import VerificationError
from infra.spec import ROOT, digest, load, prefix, tool_functions
from infra.spec import name as resource_name
from infra.stacks import durable_templates, observation_templates, owned_runtime, templates
from scripts.build_lambdas import PIPELINE_FUNCTIONS

TEAM_SIGNIN_CHANGED = (
    "Team sign-in changed: remove the identity, security and initial_access settings. "
    "Team access is now a team.toml allowlist on the UI host; see docs/DEPLOY.md section 7."
)


def load_config(path, spec):
    value = json.loads(Path(path).read_text())
    expected = {
        "status_base_url",
        "retention_days",
        "initial_reserved_concurrency",
        "investigation_paused",
        "runtime_target",
        "runtime_limits",
    }
    if {"identity", "security"} & set(value):
        raise VerificationError(TEAM_SIGNIN_CHANGED)
    if set(value) - {"fallback_email", "incident_cooldown_minutes"} != expected:
        raise ValueError(
            "Durable configuration requires status URL, retention, initial capacity and model pause"
        )
    url = value["status_base_url"]
    email = value.get("fallback_email", "fallback@example.invalid")  # optional; checked below when present
    if (
        not isinstance(url, str)
        or not re.fullmatch(r"https://[A-Za-z0-9._:-]+(?:/[A-Za-z0-9/_-]+)*/?", url)
        or not isinstance(email, str)
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
        or "," in email
        or type(value["retention_days"]) is not int
        or not 7 <= value["retention_days"] <= 365
        or type(value["investigation_paused"]) is not bool
        or type(value["initial_reserved_concurrency"]) is not int
        or not 2 <= value["initial_reserved_concurrency"] <= 1000
    ):
        raise ValueError("Invalid durable URL, fallback recipient or retention")
    from tracaus.runtime import Limits

    if value["runtime_target"] not in {"standalone", "agentcore"} or not isinstance(
        value["runtime_limits"], dict
    ):
        raise ValueError("Select standalone or agentcore with explicit runtime limits")
    Limits(**value["runtime_limits"])
    if value["runtime_target"] == "agentcore":
        owned_runtime.runtime_name(spec)
    cooldown = value.get("incident_cooldown_minutes", 15)
    if type(cooldown) is not int or not 0 <= cooldown <= 120:
        raise ValueError("incident_cooldown_minutes must be a whole number of minutes from 0 to 120")
    return value


def checked_build(path, *, expected_functions=PIPELINE_FUNCTIONS, architecture="x86_64"):
    manifest = json.loads((path / "manifest.json").read_text())
    if set(manifest["functions"]) != set(expected_functions):
        raise VerificationError("Six pipeline functions are required")
    if manifest["python"] != "3.12" or manifest["architecture"] != architecture:
        raise VerificationError("Pipeline runtime/architecture mismatch")
    expected_lock = hashlib.sha256((ROOT / "requirements/lambda.lock").read_bytes()).hexdigest()
    if manifest["lock_sha256"] != expected_lock:
        raise VerificationError("Pipeline dependency lock changed")
    for name, data in manifest["functions"].items():
        required = {str(p.relative_to(ROOT)) for p in (ROOT / "tracaus").glob("*.py")} | {
            "agent-instruction.txt",
            "schemas/fetch_logs.json",
            "schemas/fetch_metrics.json",
            "schemas/fetch_ecs.json",
            "tracaus_agentcore.py",
            "lambda_function.py",
        }
        if not required <= set(data["source_files"]):
            raise VerificationError("Build manifest omits required current runtime source")
        if data["artifact"] != name + ".zip":
            raise VerificationError("Pipeline artifact path mismatch")
        package = path / data["artifact"]
        if hashlib.sha256(package.read_bytes()).hexdigest() != data["sha256"]:
            raise VerificationError("Pipeline artifact hash mismatch")
        with zipfile.ZipFile(package) as archive:
            if hashlib.sha256(archive.read("requirements/lambda.lock")).hexdigest() != expected_lock:
                raise VerificationError("Pipeline package lock mismatch")
            for filename, expected in data["source_files"].items():
                if hashlib.sha256(archive.read(filename)).hexdigest() != expected:
                    raise VerificationError("Pipeline package source mismatch")
                if filename.startswith(("tracaus/", "schemas/")) or filename in {
                    "lambda_function.py",
                    "agent-instruction.txt",
                    "tracaus_agentcore.py",
                }:
                    source = ROOT / (
                        f"lambda/{name}/lambda_function.py" if filename == "lambda_function.py" else filename
                    )
                    if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                        raise VerificationError("Pipeline source changed after build")
    return manifest


def render(
    spec_path,
    config_path,
    output,
    build_dir=None,
    bindings_path=None,
    tool_build_dir=None,
    host_build_dir=None,
    observation_build_dir=None,
):
    spec = load(spec_path)
    config = load_config(config_path, spec)
    bindings = json.loads(bindings_path.read_text()) if bindings_path else {}
    build = checked_build(build_dir) if build_dir else None
    tool_build = release.checked_build(tool_build_dir, spec) if tool_build_dir else None
    host_build = (
        checked_build(host_build_dir, expected_functions=("incident_investigate",), architecture="arm64")
        if host_build_dir
        else None
    )
    from infra.deploy.observations import checked_build as checked_observers

    observation_build = checked_observers(observation_build_dir, spec) if observation_build_dir else None
    stages = {
        "foundation-tools": templates.foundation(spec, "tools"),
        "foundation-monitor": templates.foundation(spec, "monitor"),
        "durable-foundation": durable_templates.foundation(spec, config),
    }
    if "tool_artifacts" in bindings:
        if not tool_build or set(bindings["tool_artifacts"]) != set(tool_functions(spec)):
            raise VerificationError("Verified inventory-bound tool artifacts are required")
        for function, artifact in bindings["tool_artifacts"].items():
            expected_hash = tool_build["functions"][function]["sha256"]
            if artifact != {
                "bucket": templates.bucket_name(spec, "tools"),
                "key": f"releases/{spec['release_id']}/{expected_hash}.zip",
                "sha256": expected_hash,
                "version_id": artifact.get("version_id"),
            } or not artifact.get("version_id"):
                raise VerificationError("Tool artifact is not the pinned release object")
        secret = bindings.get("secret", {})
        if (
            set(secret) != {"arn", "version_id"}
            or not re.fullmatch(
                rf"arn:aws:secretsmanager:{spec['bedrock_region']}:{spec['account_id']}:secret:{re.escape(prefix(spec) + '/log-cursor')}-[A-Za-z0-9]{{6}}",
                secret.get("arn", ""),
            )
            or not re.fullmatch(r"[A-Za-z0-9-]{32,64}", secret.get("version_id", ""))
        ):
            raise VerificationError("A pinned customer cursor-secret version is required")
        stages["owned-tools"] = templates.tools_release(spec, bindings["tool_artifacts"], secret)
    foundation = bindings.get("foundation")
    if "observability" in spec and foundation:
        stages["observation-foundation"] = observation_templates.foundation(spec, foundation, config)
        if "observation_artifacts" in bindings:
            if not observation_build or set(bindings["observation_artifacts"]) != set(
                observation_templates.FUNCTIONS
            ):
                raise VerificationError("Complete inventory-bound observation build required")
            for function, artifact in bindings["observation_artifacts"].items():
                expected = observation_build["functions"][function]["sha256"]
                if artifact != {
                    "bucket": templates.bucket_name(spec, "monitor"),
                    "key": f"releases/{spec['release_id']}/{expected}.zip",
                    "sha256": expected,
                    "version_id": artifact.get("version_id"),
                } or not artifact.get("version_id"):
                    raise VerificationError("Observation artifact must be the pinned monitor bucket object")
            stages["observation-runtime"] = observation_templates.runtime(
                spec, foundation, bindings["observation_artifacts"], config
            )
        if "observation_versions" in bindings:
            from infra.deploy.observations import validate_versions

            validate_versions(spec, bindings["observation_versions"])
            if (
                "versions" not in bindings
                or "observation-runtime" not in stages
                or not build
                or not bindings.get("artifacts")
            ):
                raise VerificationError("Observation schedules require verified pipeline/runtime bindings")
            stages["observations"] = observation_templates.active(
                spec,
                foundation,
                bindings["observation_versions"],
                bindings["versions"],
                runtime_target=config["runtime_target"],
                agentcore=bindings.get("agentcore"),
            )
    artifacts = bindings.get("artifacts")
    if foundation and artifacts:
        if not build or set(artifacts) != set(PIPELINE_FUNCTIONS):
            raise VerificationError("Complete verified pipeline build required for runtime")
        for function, item in artifacts.items():
            expected_hash = build["functions"][function]["sha256"]
            if item != {
                "bucket": templates.bucket_name(spec, "monitor"),
                "key": f"releases/{spec['release_id']}/{expected_hash}.zip",
                "version_id": item.get("version_id"),
                "sha256": expected_hash,
            } or not item.get("version_id"):
                raise VerificationError("Pipeline artifact is not the pinned monitor bucket object")
        owned_runtime.validate_bindings(spec, config, bindings)
        if config["runtime_target"] == "agentcore":
            host = bindings.get("host_artifact")
            if not host_build:
                raise VerificationError("AgentCore requires an ARM64 host build")
            expected = host_build["functions"]["incident_investigate"]["sha256"]
            if (
                not host
                or host
                != {
                    "bucket": templates.bucket_name(spec, "tools"),
                    "key": f"releases/{spec['release_id']}/{expected}.zip",
                    "version_id": host.get("version_id"),
                    "sha256": expected,
                }
                or not host.get("version_id")
            ):
                raise VerificationError("AgentCore requires the verified host ZIP in the tools region")
            stages["agentcore-runtime"] = owned_runtime.agentcore_release(spec, config, bindings, host)
            candidate = bindings.get("agentcore_candidate")
            if candidate:
                runtime_id = candidate.get("RuntimeId", "")
                if not re.fullmatch(
                    re.escape(owned_runtime.runtime_name(spec) + "-") + r"[A-Za-z0-9]{10}",
                    runtime_id,
                ):
                    raise VerificationError("AgentCore candidate belongs to another release")
                stages["agentcore-endpoint"] = owned_runtime.agentcore_endpoint(
                    spec, runtime_id, candidate["RuntimeVersion"]
                )
            if "agentcore" not in bindings:
                # Render the host stages before its immutable endpoint exists.
                artifacts = None
        if artifacts:
            stages["durable-runtime"] = durable_templates.runtime(
                spec, config, artifacts, foundation, owned_bindings=bindings
            )
        if artifacts and "versions" in bindings:
            versions = bindings["versions"]
            if set(versions) != {
                name + "VersionArn"
                for name in ("Ingress", "Dispatch", "Investigate", "Initial", "Report", "Reconcile")
            }:
                raise VerificationError("Incomplete pipeline version bindings")
            for function, logical in (
                ("incident_ingress", "Ingress"),
                ("incident_dispatch", "Dispatch"),
                ("incident_investigate", "Investigate"),
                ("incident_initial", "Initial"),
                ("incident_report", "Report"),
                ("incident_reconcile", "Reconcile"),
            ):
                arn = versions[logical + "VersionArn"]
                expected = (
                    f"arn:aws:lambda:{spec['monitor_region']}:{spec['account_id']}:function:"
                    f"{resource_name(spec, function.replace('_', '-'), True)}:"
                )
                if not isinstance(arn, str) or not re.fullmatch(re.escape(expected) + r"[1-9][0-9]*", arn):
                    raise VerificationError("Pipeline version must be a qualified Lambda ARN in this account")
            stages["routing"] = durable_templates.active_routing(
                spec,
                foundation,
                versions,
                config["investigation_paused"],
                config=config,
                owned_bindings=bindings,
            )
    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    bundle = {
        "version": 3,
        "source_sha": source_sha,
        "source_dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
        ),
        "spec": spec,
        "config": config,
        "bindings": bindings,
        "build": build,
        "tool_build": tool_build,
        "host_build": host_build,
        **({"observation_build": observation_build} if "observability" in spec else {}),
        "stages": {
            stage: {
                "stack": resource_name(
                    spec,
                    stage,
                    stage
                    in {
                        "durable-runtime",
                        "owned-tools",
                        "agentcore-runtime",
                        "agentcore-endpoint",
                        "observation-runtime",
                    },
                ),
                "region": spec["bedrock_region"]
                if stage
                in {
                    "foundation-tools",
                    "owned-tools",
                    "agentcore-runtime",
                    "agentcore-endpoint",
                }
                else spec["monitor_region"],
                "create_only": stage
                in {
                    "durable-runtime",
                    "owned-tools",
                    "agentcore-runtime",
                    "agentcore-endpoint",
                    "observation-runtime",
                },
                "template_hash": templates.template_hash(value),
            }
            for stage, value in stages.items()
        },
    }
    bundle["review_hash"] = digest(bundle)
    output.mkdir(parents=True, exist_ok=True)
    for stage, value in stages.items():
        if len(templates.template_body(value).encode()) > templates.TEMPLATE_LIMIT:
            raise VerificationError("Durable template exceeds inline change-set limit")
        release.write_json(output / f"{stage}.json", value)
    release.write_json(output / "bundle.json", bundle)
    if "observability" in spec:
        release.write_json(
            output / "coverage.json", observation_templates.coverage_manifest(spec, stages.values())
        )
    return bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--bindings", type=Path)
    parser.add_argument("--tool-build-dir", type=Path)
    parser.add_argument("--host-build-dir", type=Path)
    parser.add_argument("--observation-build-dir", type=Path)
    args = parser.parse_args()
    bundle = render(
        args.spec,
        args.config,
        args.output,
        args.build_dir,
        args.bindings,
        args.tool_build_dir,
        args.host_build_dir,
        args.observation_build_dir,
    )
    print(
        json.dumps(
            {
                "review_hash": bundle["review_hash"],
                "stages": list(bundle["stages"]),
                "source_dirty": bundle["source_dirty"],
            }
        )
    )


if __name__ == "__main__":
    main()
