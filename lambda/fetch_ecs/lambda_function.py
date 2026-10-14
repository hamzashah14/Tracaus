"""Bedrock Agent tool: the state of one ECS service around an incident.

Deployments, service events, recently stopped tasks, the task definition and, with an ALB in front, the
target group's health. Read only. The cluster, service and target group come from the packaged
ecs-targets.json, never from the model."""

import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from tracaus.metrics import allowed_instance, ecs_targets
from tracaus.time import parse_utc
from tracaus.transport import bounded_envelope, error_result, fits, parameters

MONITOR_REGION = os.environ.get("MONITOR_REGION") or os.environ.get("AWS_REGION")
BOTO_CONFIG = Config(connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1})

DEFAULT_WINDOW_MINUTES, MAX_WINDOW_MINUTES = 30, 180
MAX_DEPLOYMENTS, MAX_EVENTS, MAX_TASKS, MAX_CONTAINERS, MAX_TARGETS = 5, 20, 10, 8, 20
MAX_TEXT = 300
MAX_PAGES = 5  # of 100 stopped-task ARNs each; ECS keeps stopped tasks for about an hour
TIME_FMT = "%Y-%m-%d %H:%M:%S"


class BadInput(ValueError):
    pass


def _int_param(params, name, default, lo, hi):
    raw = params.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        raise BadInput(f"{name} must be a number, got {raw!r}.")
    return max(lo, min(hi, value))


def clip(text, limit=MAX_TEXT):
    if text is None:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def cap(rows, limit, notes):
    """The first `limit` rows. A row left out is noted, so the answer is marked partial rather than complete."""
    if len(rows) > limit:
        notes.append(limit)
    return rows[:limit]


def stamp(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def short(arn):
    return str(arn).rsplit("/", 1)[-1]


def resolve_window(params, now):
    raw = params.get("time_string")
    if not raw:
        return None, now - timedelta(minutes=DEFAULT_WINDOW_MINUTES), now
    try:
        anchor = parse_utc(str(raw))
    except ValueError:
        raise BadInput("time_string must be formatted as YYYY-MM-DD HH:MM:SS (UTC).")
    if anchor > now:
        raise BadInput(f"time_string is in the future (now is {now.strftime(TIME_FMT)} UTC).")
    window = timedelta(
        minutes=_int_param(params, "window_minutes", DEFAULT_WINDOW_MINUTES, 1, MAX_WINDOW_MINUTES)
    )
    return anchor, anchor - window, min(anchor + window, now)


def service_block(service, notes):
    deployments = sorted(service.get("deployments", []), key=lambda d: d["createdAt"], reverse=True)
    return {
        "status": service["status"],
        "desired_count": service.get("desiredCount", 0),
        "running_count": service.get("runningCount", 0),
        "pending_count": service.get("pendingCount", 0),
        "task_definition": short(service["taskDefinition"]),
        "deployments": [
            {
                "status": d.get("status"),
                "rollout_state": d.get("rolloutState"),
                "rollout_state_reason": clip(d.get("rolloutStateReason")),
                "task_definition": short(d["taskDefinition"]),
                "created_at": stamp(d.get("createdAt")),
                "desired_count": d.get("desiredCount", 0),
                "running_count": d.get("runningCount", 0),
                "pending_count": d.get("pendingCount", 0),
                "failed_tasks": d.get("failedTasks", 0),
            }
            for d in cap(deployments, MAX_DEPLOYMENTS, notes)
        ],
    }


def service_events(service, start, end, notes):
    inside = [e for e in service.get("events", []) if start <= e["createdAt"] <= end]
    inside.sort(key=lambda e: e["createdAt"])
    newest = cap(inside[::-1], MAX_EVENTS, notes)[::-1]
    return [{"at": stamp(e["createdAt"]), "message": clip(e.get("message"))} for e in newest]


def stopped_tasks(client, cluster, name, start, end, notes):
    arns, token = [], None
    for _ in range(MAX_PAGES):
        page = client.list_tasks(
            cluster=cluster,
            serviceName=name,
            desiredStatus="STOPPED",
            maxResults=100,
            **({"nextToken": token} if token else {}),
        )
        arns += page["taskArns"]
        token = page.get("nextToken")
        if not token:
            break
    if token:
        notes.append(MAX_PAGES)  # more stopped tasks exist than the pages read
    tasks = []
    for first in range(0, len(arns), 100):  # DescribeTasks takes at most 100 at a time
        tasks += client.describe_tasks(cluster=cluster, tasks=arns[first : first + 100])["tasks"]
    # A STOPPED task with no stop time is shown with a null time rather than dropped: it is still evidence.
    inside = [
        t
        for t in tasks
        if (start <= t["stoppedAt"] <= end if t.get("stoppedAt") else t.get("lastStatus") == "STOPPED")
    ]
    inside.sort(key=lambda t: t.get("stoppedAt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return [
        {
            "task": short(t["taskArn"]),
            "stop_code": t.get("stopCode"),
            "stopped_reason": clip(t.get("stoppedReason")),
            "stopped_at": stamp(t.get("stoppedAt")),
            "task_definition": short(t["taskDefinitionArn"]) if t.get("taskDefinitionArn") else None,
            "containers": [
                {"name": c.get("name", ""), "exit_code": c.get("exitCode"), "reason": clip(c.get("reason"))}
                for c in cap(t.get("containers", []), MAX_CONTAINERS, notes)
            ],
        }
        for t in cap(inside, MAX_TASKS, notes)
    ]


def task_definition_block(client, arn, notes):
    """An allowlist. Task definitions can hold environment values and secret references: none leave here."""
    td = client.describe_task_definition(taskDefinition=arn)["taskDefinition"]
    return {
        "family": td["family"],
        "revision": td["revision"],
        "cpu": td.get("cpu"),
        "memory": td.get("memory"),
        "containers": [
            {
                "name": c["name"],
                "image": clip(c.get("image"), 200),
                "cpu": c.get("cpu", 0),
                "memory": c.get("memory"),
                "memory_reservation": c.get("memoryReservation"),
                "essential": c.get("essential", True),
                "health_check": "healthCheck" in c,
                "log_driver": (c.get("logConfiguration") or {}).get("logDriver"),
            }
            for c in cap(td.get("containerDefinitions", []), MAX_CONTAINERS, notes)
        ],
    }


def target_health(group_arn, notes):
    elb = boto3.client("elbv2", region_name=MONITOR_REGION, config=BOTO_CONFIG)
    descriptions = elb.describe_target_health(TargetGroupArn=group_arn)["TargetHealthDescriptions"]
    counts = {}
    for item in descriptions:
        state = item["TargetHealth"]["State"]
        counts[state] = counts.get(state, 0) + 1
    return {
        "status": "ok",
        "counts": counts,
        "targets": [
            {
                "state": item["TargetHealth"]["State"],
                "reason": item["TargetHealth"].get("Reason"),
                "description": clip(item["TargetHealth"].get("Description")),
                "port": item.get("Target", {}).get("Port"),
            }
            for item in cap(descriptions, MAX_TARGETS, notes)
        ],
    }


def trim(event, result):
    """Drop the least useful rows until the answer fits the transport budget, and say so."""
    while not fits(event, result):
        lists = [
            (result["service_events"], 0),  # oldest first
            (result["stopped_tasks"], -1),  # newest first, so the oldest is last
            (result.get("load_balancer", {}).get("targets", []), -1),
        ]
        rows, index = max(lists, key=lambda pair: len(pair[0]))
        if not rows:
            raise BadInput("The service state exceeds the response budget. Narrow the window.")
        rows.pop(index)
        result.update(truncated=True, complete=False, status="partial")
    return result


def search(params, event=None):
    iid = allowed_instance(params.get("instance_id"))
    where = ecs_targets().get(iid)
    if not iid.startswith("ecs-") or where is None:
        raise BadInput("fetch_ecs reads ECS services only; this target is not a configured ECS service.")
    now = datetime.now(timezone.utc)
    anchor, start, end = resolve_window(params, now)
    base = {
        "instance_id": iid,
        "ecs_service": f"{where['cluster']}/{where['service']}",
        "window_start": start.strftime(TIME_FMT),
        "window_end": end.strftime(TIME_FMT),
        "complete": True,
        "truncated": False,
    }
    if anchor:
        base["incident_time"] = anchor.strftime(TIME_FMT)
    client = boto3.client("ecs", region_name=MONITOR_REGION, config=BOTO_CONFIG)
    try:
        response = client.describe_services(cluster=where["cluster"], services=[where["service"]])
    except ClientError as error:
        if error.response["Error"]["Code"] != "ClusterNotFoundException":
            raise
        response = {"services": []}
    service = next((s for s in response["services"] if s.get("serviceName") == where["service"]), None)
    if service is None or service.get("status") == "INACTIVE":
        return {
            "status": "no_data",
            **base,
            "message": "The ECS service was not found or is INACTIVE; it may have been deleted.",
        }
    notes = []
    result = {
        "status": "ok",
        **base,
        "service": service_block(service, notes),
        "service_events": service_events(service, start, end, notes),
        "stopped_tasks": stopped_tasks(client, where["cluster"], where["service"], start, end, notes),
        "task_definition": task_definition_block(client, service["taskDefinition"], notes),
    }
    if where["load_balancer"]:
        try:
            result["load_balancer"] = target_health(where["load_balancer"]["target_group_arn"], notes)
        except ClientError:
            result["load_balancer"] = {"status": "unavailable", "counts": {}, "targets": []}
            result["complete"] = False  # the model must declare the missing target health
    if notes:  # any row left out, as trim() does for the transport budget
        result.update(truncated=True, complete=False, status="partial")
    return trim(event or {}, result)


def lambda_handler(event, context):
    status = 200
    try:
        params = parameters(event)
        from tracaus.tool_deadline import remaining

        remaining(event, context)
        result = search(params, event)
    except (BadInput, ValueError) as exc:
        result, status = error_result("INVALID_REQUEST", str(exc)), 400
    except ClientError:
        result, status = (
            error_result(
                "ECS_ACCESS_FAILED",
                "ECS could not return the service state. Check access and service availability.",
            ),
            502,
        )
    except Exception:
        result, status = (
            error_result(
                "ECS_FAILED",
                "ECS lookup failed. Check deployment configuration and service availability.",
            ),
            502,
        )
    return bounded_envelope(event, result, status)
