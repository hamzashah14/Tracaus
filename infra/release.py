"""Release bundles bind source, immutable S3 object versions, templates and evidence."""

import hashlib
import json
import zipfile
from datetime import datetime, timezone

from infra import templates
from infra.spec import (
    ROOT,
    canary_tools,
    digest,
    ecs_targets,
    existing_log_groups,
    log_groups,
    metric_catalog,
    tool_functions,
)
from infra.verify import VerificationError

SEALED_POLICY = {"Statement": [{"Effect": "Deny", "Action": "Update:*", "Principal": "*", "Resource": "*"}]}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def checked_build(directory, spec):
    manifest = json.loads((directory / "manifest.json").read_text())
    if set(manifest["functions"]) != set(tool_functions(spec)):
        raise VerificationError("Release requires every read-only tool artifact the deployment needs")
    if manifest["python"] != "3.12" or manifest["architecture"] != "x86_64":
        raise VerificationError("Unsupported build runtime/architecture")
    lock_hash = hashlib.sha256((ROOT / "requirements/lambda.lock").read_bytes()).hexdigest()
    if manifest.get("lock_sha256") != lock_hash:
        raise VerificationError("Dependency lock changed after build; rebuild before rendering")
    for function, item in manifest["functions"].items():
        required = {str(p.relative_to(ROOT)) for p in (ROOT / "tracaus").glob("*.py")} | {
            "agent-instruction.txt",
            "schemas/fetch_logs.json",
            "schemas/fetch_metrics.json",
            "tracaus_agentcore.py",
            "lambda_function.py",
            *(["schemas/fetch_ecs.json", "config/ecs-targets.json"] if ecs_targets(spec) else []),
        }
        if not required <= set(item["source_files"]):
            raise VerificationError("Build manifest omits required current source")
        if item["artifact"] != f"{function}.zip":
            raise VerificationError("Invalid artifact path")
        path = directory / item["artifact"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise VerificationError("Artifact hash differs from build manifest")
        with zipfile.ZipFile(path) as archive:
            if hashlib.sha256(archive.read("requirements/lambda.lock")).hexdigest() != lock_hash:
                raise VerificationError("Packaged dependency lock differs from current source")
            if json.loads(archive.read("config/metric-catalog.json")) != metric_catalog(spec):
                raise VerificationError("Packaged metric catalog differs from deployment inventory")
            if json.loads(archive.read("config/log-scope.json")) != log_groups(spec):
                raise VerificationError("Packaged log scope differs from deployment inventory")
            existing = existing_log_groups(spec)
            if ("config/existing-log-groups.json" in archive.namelist()) != bool(existing) or (
                existing and json.loads(archive.read("config/existing-log-groups.json")) != existing
            ):
                raise VerificationError("Packaged existing log groups differ from deployment inventory")
            ecs = ecs_targets(spec)
            if ("config/ecs-targets.json" in archive.namelist()) != bool(ecs) or (
                ecs and json.loads(archive.read("config/ecs-targets.json")) != ecs
            ):
                raise VerificationError("Packaged ECS targets differ from deployment inventory")
            for filename, expected in item["source_files"].items():
                if hashlib.sha256(archive.read(filename)).hexdigest() != expected:
                    raise VerificationError("Packaged source differs from build manifest")
                source = ROOT / (
                    f"lambda/{function}/lambda_function.py" if filename == "lambda_function.py" else filename
                )
                if filename.startswith(("tracaus/", "schemas/")) or filename in {
                    "lambda_function.py",
                    "agent-instruction.txt",
                    "tracaus_agentcore.py",
                }:
                    if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                        raise VerificationError("Source changed after build; rebuild before rendering")
    return manifest


def change_set_digest(response):
    return digest(
        {
            key: response.get(key)
            for key in ("ChangeSetId", "StackId", "Changes", "Parameters", "Capabilities", "RoleARN", "Tags")
        }
    )


def inspect_change_set(bundle, stage, change_set, clients):
    info = bundle["stages"][stage]
    client = clients("cloudformation", info["region"])
    result = client.describe_change_set(StackName=info["stack"], ChangeSetName=change_set)
    if result.get("NextToken"):
        raise VerificationError("Paginated change set needs complete review; execution is blocked")
    if result["Status"] != "CREATE_COMPLETE" or result["ExecutionStatus"] != "AVAILABLE":
        raise VerificationError("Change set is not ready for execution")
    body = client.get_template(StackName=info["stack"], ChangeSetName=change_set)["TemplateBody"]
    if isinstance(body, str):
        body = json.loads(body)
    if templates.template_hash(body) != info["template_hash"]:
        raise VerificationError("CloudFormation template differs from reviewed bundle")
    if result.get("RoleARN") != bundle["spec"]["deployment_role_arn"]:
        raise VerificationError("CloudFormation execution role differs from the plan")
    return result, change_set_digest(result)


def require_receipt(bundle, record):
    if not isinstance(record, dict):
        raise VerificationError("Promotion requires a candidate receipt")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(record["checked_at"])).total_seconds()
    if (
        record.get("bundle_hash") != bundle["review_hash"]
        or record.get("status") != "PASS"
        or not 0 <= age <= 3600
        or not canary_tools(bundle["spec"]) <= set(record.get("canary_tools", []))
    ):
        raise VerificationError("Promotion requires a passing, recent canary receipt for this exact bundle")
