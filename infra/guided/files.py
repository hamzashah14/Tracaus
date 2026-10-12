"""Turn discovered facts and answers into the three settings files, and report what is still missing.

`build` is pure. `write` validates the files with the same loaders `dry-run` uses before it replaces anything,
keeps the previous copies, and refuses to touch a deployment that has already started."""

import contextlib
import copy
import json
import os
import re
import shutil
from pathlib import Path

from infra.automation import private_json
from infra.spec import ROOT

NAMES = ("deployment.json", "runtime.json", "automation.json")
MAX_TARGETS = 10


class FilesError(Exception):
    """The settings cannot be built or written. The message never carries a submitted value."""


def ecs_id(service, taken):
    """A target ID for an ECS service: `ecs-` plus up to 24 of [a-z0-9_], unique among `taken`."""
    base = re.sub(r"[^a-z0-9_]", "", service.lower().replace("-", "_"))[:24] or "service"
    candidate, number = f"ecs-{base}", 2
    while candidate in taken:
        suffix = f"_{number}"
        candidate = f"ecs-{base[: 24 - len(suffix)]}{suffix}"
        number += 1
    return candidate


def _example(name):
    return json.loads((ROOT / "examples" / name).read_text())


def build(choices):
    """`{"deployment", "runtime", "automation"}` from the wizard's choices. Pure: no file, no AWS call."""
    ec2, ecs = choices.get("ec2", []), choices.get("ecs", [])
    if not ec2 and not ecs:
        raise FilesError("Choose at least one EC2 instance or ECS service.")
    if len(ec2) + len(ecs) > MAX_TARGETS:
        raise FilesError(f"Choose at most {MAX_TARGETS} targets in total.")
    base = _example("deployment.example.json")
    instances = []
    for item in ec2:
        entry = {
            "id": item["id"],
            "log_groups": item.get("log_groups", []),
            "disk_path": item["disk_path"],
            "resource_alarms": True,
            "nginx_alarm": bool(item.get("nginx")),
            "process_exe": item.get("process_exe"),
        }
        if item.get("existing_log_groups"):
            entry["existing_log_groups"] = [
                {"name": name, "streams": "instance"} for name in item["existing_log_groups"]
            ]
        if item.get("log_files"):
            entry["log_files"] = [dict(file) for file in item["log_files"]]
        instances.append(entry)
    roles = choices["roles"]
    deployment = {
        "project": choices["project"],
        "environment": choices["environment"],
        "account_id": choices["account_id"],
        "monitor_region": choices["region"],
        "bedrock_region": choices["bedrock_region"],
        "release_id": choices["release_id"],
        "executor_mode": "qualified",
        "notification_emails": list(choices["emails"]),
        "reserved_concurrency": None,
        "maintenance_mode": False,
        "log_retention_days": choices.get("retention_days", 30),
        "ui_principal_arn": roles["ui"],
        "ci_principal_arn": roles["operator"],
        "deployment_role_arn": roles["execution"],
        "instances": instances,
        "log_segment": "",
        "reference_only": False,
    }
    model = choices["model"]
    deployment["model_id"] = model["model_id"]
    if model["provider"] == "bedrock":
        deployment["model_arns"] = list(model["model_arns"])
    else:
        deployment["model_provider"] = "model_api"
        deployment["model_api"] = {"protocol": model["protocol"], "base_url": model["base_url"]}
    if any(item["nginx_alarm"] for item in instances):
        deployment["nginx_filters"] = copy.deepcopy(base["nginx_filters"])
    if ecs:
        deployment["ecs_services"] = [_ecs_entry(item) for item in ecs]
    runtime = _example("durable.example.json")
    runtime.pop("fallback_email", None)  # omitted: the fallback topics then use the report addresses
    runtime["status_base_url"] = choices["status_base_url"]
    automation = {
        "version": 1,
        "spec": "deployment.json",
        "runtime_config": "runtime.json",
        "profile": choices["profile"],
        "wheelhouse": str(ROOT / ".build/wheels"),
    }
    return {"deployment": deployment, "runtime": runtime, "automation": automation}


def _ecs_entry(item):
    entry = {
        "id": item["id"],
        "cluster": item["cluster"],
        "service": item["service"],
        "load_balancer": item["load_balancer"],
        "resource_alarms": True,
        "container_insights": bool(item.get("container_insights")),
    }
    if item.get("log_groups"):
        entry["existing_log_groups"] = [{"name": name} for name in item["log_groups"]]
    return entry


def validate(directory):
    """Load the three files with the loaders `dry-run` uses. Raises FilesError without the offending value."""
    from infra.durable import load_config
    from infra.spec import load

    directory = Path(directory)
    try:
        spec = load(directory / "deployment.json")
        load_config(directory / "runtime.json", spec)
        automation = json.loads((directory / "automation.json").read_text())
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise FilesError(f"The settings do not validate: {type(error).__name__}: {_safe(error)}") from None
    if set(automation) != {"version", "spec", "runtime_config", "profile", "wheelhouse"}:
        raise FilesError("The automation settings have unexpected fields.")
    return spec


def _safe(error):
    """Loader messages name fields, never values; still keep them to one short line."""
    return " ".join(str(error).split())[:160]


def write(directory, built):
    """Validate the three files in a staging folder, then replace the real ones.

    Existing copies are kept as `<name>.previous`. A deployment that already has a journal is left alone."""
    directory = Path(directory)
    if (directory / "state.json").exists():
        raise FilesError("A deployment already started in this folder. Use 'refresh' for a new release.")
    staging = directory / ".staging"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        for name, key in zip(NAMES, ("deployment", "runtime", "automation"), strict=True):
            private_json(staging / name, built[key])
        validate(staging)
        for name in NAMES:
            target = directory / name
            if target.exists():
                if target.is_symlink():
                    raise FilesError("A settings file is a symlink; remove it first.")
                shutil.copy2(target, directory / f"{name}.previous")
                os.chmod(directory / f"{name}.previous", 0o600)
            os.replace(staging / name, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    # Paths in automation.json are relative to it, so the staging copy is identical to the final one.
    with contextlib.suppress(OSError):
        os.chmod(directory, 0o700)


def readiness(choices, facts, examples="collector-examples"):
    """What is found, missing or unknown on each target, with the exact action for the user.

    `facts` holds `agent` ({instance: cwagent_facts}), `groups` ({name: True/False/None}) and, for ECS,
    nothing else: the choices already carry the discovered load balancer and Container Insights flags."""
    report = []
    for item in choices.get("ec2", []):
        agent = facts.get("agent", {}).get(item["id"], {"state": "unknown"})
        file = f"{examples}/{item['id']}.json"
        checks = [
            _check("memory metric", agent, agent.get("memory"), f"Install the CloudWatch agent with {file}."),
            _disk(item, agent, file),
        ]
        if item.get("process_exe"):
            checks.append(
                _check(
                    f"process metric ({item['process_exe']})",
                    agent,
                    item["process_exe"] in agent.get("exes", []),
                    f"Add the procstat entry from {file} and restart the agent.",
                )
            )
        for name in item.get("existing_log_groups", []):
            state = facts.get("groups", {}).get(name)
            checks.append(_group(name, state))
        for name in item.get("log_groups", []):
            checks.append(
                {
                    "name": f"log group {name}",
                    "state": "unknown",
                    "action": f"Created at deploy. Ship your log file into it with {file} (log_files).",
                }
            )
        if item.get("nginx"):
            checks.append(
                {
                    "name": "nginx logs",
                    "state": "unknown",
                    "action": f"Ship /var/log/nginx/access.log and error.log with {file}.",
                }
            )
        report.append({"target": item["id"], "label": item.get("label", ""), "checks": checks})
    for item in choices.get("ecs", []):
        checks = [_group(name, facts.get("groups", {}).get(name)) for name in item.get("log_groups", [])]
        if not item.get("log_groups"):
            checks.append(
                {"name": "log groups", "state": "missing", "action": "No awslogs group; logs stay unread."}
            )
        balancer = item["load_balancer"]
        checks.append(
            {
                "name": "load balancer",
                "state": "found",
                "action": "ALB metrics and alarms are on."
                if balancer
                else "No ALB: load balancer metrics and alarms are off.",
            }
        )
        report.append({"target": item["id"], "label": item.get("label", ""), "checks": checks})
    return report


def _check(name, agent, present, action):
    if agent.get("state") == "unknown":
        return {
            "name": name,
            "state": "unknown",
            "action": "Could not read CloudWatch metrics (access denied).",
        }
    return {"name": name, "state": "found" if present else "missing", "action": "" if present else action}


def _disk(item, agent, file):
    path = item["disk_path"]
    if agent.get("state") == "unknown":
        return _check(f"disk metric ({path})", agent, False, "")
    if path in agent.get("disk_paths", []):
        return _check(f"disk metric ({path})", agent, True, "")
    if path in agent.get("disk_wide", []):
        action = (
            "The disk metric has extra dimensions; set aggregation_dimensions [InstanceId, path] as in "
            + file
        )
    else:
        action = f"Add the disk entry for {path} from {file} and restart the agent."
    return _check(f"disk metric ({path})", agent, False, action)


def _group(name, state):
    if state is None:
        return {"name": f"log group {name}", "state": "unknown", "action": "Could not check that it exists."}
    return {
        "name": f"log group {name}",
        "state": "found" if state else "missing",
        "action": "" if state else "The group does not exist yet; start the service or create the group.",
    }
