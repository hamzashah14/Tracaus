"""Strict, secret-free deployment inventory shared by plans and verification."""

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
MAX_TARGETS = 10  # EC2 instances and ECS services together


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def ecs_services(spec):
    return spec.get("ecs_services", [])


def targets(spec):
    """Every target ID the deployment may investigate: EC2 instances, then ECS services."""
    return [item["id"] for item in spec["instances"]] + [item["id"] for item in ecs_services(spec)]


def tool_functions(spec):
    """The read-only tool Lambdas this deployment needs. `fetch_ecs` exists only with ECS services."""
    return ("fetch_logs", "fetch_metrics") + (("fetch_ecs",) if ecs_services(spec) else ())


def canary_target(spec):
    """The target the paid canary investigates: the first EC2 instance, else the first ECS service."""
    return spec["instances"][0]["id"] if spec["instances"] else ecs_services(spec)[0]["id"]


def canary_tools(spec):
    """Tools the canary must prove. EC2 targets need logs and metrics; an ECS-only deployment, state and metrics."""
    return {"fetch_logs", "fetch_metrics"} if spec["instances"] else {"fetch_ecs", "fetch_metrics"}


def event_rules(spec):
    """{rule name suffix: template key} for the EventBridge rules that exist: EC2 stops, ECS task stops."""
    rules = {}
    if spec["instances"]:
        rules["ec2-down"] = "Ec2Down"
    if ecs_services(spec):
        rules["ecs-down"] = "EcsDown"
    return rules


TOOL_LOGICAL = {"fetch_logs": "Logs", "fetch_metrics": "Metrics", "fetch_ecs": "Ecs"}


def tool_bindings(spec):
    """The stack output names that carry each tool's pinned version ARN."""
    return [TOOL_LOGICAL[function] + "VersionArn" for function in tool_functions(spec)]


def alb_dimensions(load_balancer):
    """CloudWatch dimension values for an ALB and its target group, taken from their ARNs."""
    return {
        "LoadBalancer": load_balancer["load_balancer_arn"].split(":loadbalancer/", 1)[1],
        "TargetGroup": load_balancer["target_group_arn"].rsplit(":", 1)[1],
    }


def ecs_targets(spec):
    """{target ID: where its cluster, service and optional ALB are}; packaged into the tools and the ingress."""
    return {
        service["id"]: {
            "cluster": service["cluster"],
            "service": service["service"],
            "cluster_arn": f"arn:aws:ecs:{spec['monitor_region']}:{spec['account_id']}:cluster/{service['cluster']}",
            "container_insights": service.get("container_insights", False),
            "load_balancer": (
                None
                if service["load_balancer"] is None
                else {
                    **alb_dimensions(service["load_balancer"]),
                    "target_group_arn": service["load_balancer"]["target_group_arn"],
                }
            ),
        }
        for service in ecs_services(spec)
    }


def load(path):
    value = json.loads(Path(path).read_text())
    schema = json.loads((ROOT / "infra/deployment.schema.json").read_text())
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: str(e.path))
    if errors:
        # No submitted values (which may contain secrets) in diagnostics.
        raise ValueError(f"Invalid deployment field: {'.'.join(map(str, errors[0].path)) or 'root'}")
    ids = [item["id"] for item in value["instances"]]
    everything = targets(value)
    if len(set(everything)) != len(everything):
        raise ValueError("Duplicate target IDs")
    if not everything:
        raise ValueError("List at least one EC2 instance or ECS service")
    if any(item["nginx_alarm"] for item in value["instances"]) and "nginx_filters" not in value:
        raise ValueError("nginx_filters is required when an instance has nginx_alarm")
    if len(everything) > MAX_TARGETS:
        raise ValueError(f"At most {MAX_TARGETS} targets (EC2 instances and ECS services together)")
    if value["reserved_concurrency"] == 0 and not value["maintenance_mode"]:
        raise ValueError("Zero concurrency requires maintenance mode")
    owned, modes = log_prefix(value) + "/", {}
    for item in value["instances"]:
        groups = item.get("existing_log_groups", [])
        names = [group["name"] for group in groups]
        if not item["log_groups"] and not names:
            raise ValueError("Every instance needs log_groups or existing_log_groups")
        if len(set(names)) != len(names) or any(
            name.startswith(owned) or name.endswith("/") or name in item["log_groups"] for name in names
        ):
            raise ValueError(
                "Invalid existing_log_groups: names must be unique and not one of Tracaus's own groups"
            )
        for group in groups:
            modes.setdefault(group["name"], []).append(group.get("streams", "instance"))
    services_seen = set()
    for service in ecs_services(value):
        where = (service["cluster"], service["service"])
        if where in services_seen:
            raise ValueError("Each ECS cluster and service may be listed once")
        services_seen.add(where)
        balancer = service["load_balancer"]
        if balancer and any(
            arn.split(":")[3:5] != [value["monitor_region"], value["account_id"]] for arn in balancer.values()
        ):
            raise ValueError("load_balancer ARNs must belong to the deployment's account and monitor_region")
        names = [group["name"] for group in service.get("existing_log_groups", [])]
        if len(set(names)) != len(names) or any(
            name.startswith(owned) or name.endswith("/") for name in names
        ):
            raise ValueError(
                "Invalid existing_log_groups: names must be unique and not one of Tracaus's own groups"
            )
        for name in names:
            modes.setdefault(name, []).append("all")
    if any("all" in seen and len(seen) > 1 for seen in modes.values()):
        raise ValueError("An existing log group read with streams=all can belong to one instance only")
    account = value["account_id"]
    for field in ("ui_principal_arn", "ci_principal_arn", "deployment_role_arn"):
        if f":{account}:role/" not in value[field]:
            raise ValueError(f"{field} must be an explicit role in the target account")
    if len({value[f] for f in ("ui_principal_arn", "ci_principal_arn", "deployment_role_arn")}) != 3:
        raise ValueError("UI, CI and deployment identities must be distinct")
    # Schema guarantees model_arns exists exactly when the provider is Bedrock.
    for arn in value.get("model_arns", []):
        if ":foundation-model/" not in arn and f":{account}:" not in arn:
            raise ValueError("Model profile must belong to the target account")
    if (
        value.get("model_provider", "bedrock") == "bedrock"
        and value["model_id"].startswith("arn:")
        and value["model_id"] not in value["model_arns"]
    ):
        raise ValueError("Model ARN must be included in model_arns")
    if "observability" in value:
        from tracaus.observation_config import validate

        validate(value["observability"], value["instances"])
        descriptors = {
            d["id"]: d for d in alarm_descriptors({k: v for k, v in value.items() if k != "observability"})
        }
        if any(
            s["collector_metric_id"] not in descriptors
            or descriptors[s["collector_metric_id"]]["namespace"] != "CWAgent"
            or descriptors[s["collector_metric_id"]]["instance_id"] != s["instance_id"]
            for s in value["observability"]["services"]
        ):
            raise ValueError("Collector heartbeat must reference an exact declared metric")
        if {s["instance_id"] for s in value["observability"]["services"]} != set(ids):
            raise ValueError("Every mandatory inventory instance needs observed service coverage")
    for item in value["instances"]:
        known = (set(item["log_groups"]) - shipped(value, item)) | {
            group["name"] for group in item.get("existing_log_groups", [])
        }
        pairs = [(entry["group"], entry["file"]) for entry in item.get("log_files", [])]
        if len(set(pairs)) != len(pairs) or any(g not in known or ".." in f.split("/") for g, f in pairs):
            raise ValueError("Invalid log_files: unique files, each for one of the instance's own log groups")
    return value


def prefix(spec):
    return f"{spec['project']}-{spec['environment']}"


def log_prefix(spec):
    segment = spec["log_segment"]
    return f"/{spec['project']}/{spec['environment']}" + (f"/{segment}" if segment else "")


def tags(spec, release=False):
    value = {
        "Project": spec["project"],
        "Environment": spec["environment"],
        "ManagedBy": "tracaus-cloudformation",
    }
    if release:
        value["Release"] = spec["release_id"]
    return value


def name(spec, suffix, release=False):
    value = f"{prefix(spec)}-{spec['release_id']}-" if release else f"{prefix(spec)}-"
    value += suffix
    if len(value) > 64:
        raise ValueError("Resource name exceeds 64 characters")
    return value


def recipients(spec):
    """Report recipients, sorted: `notification_emails`, or the single `notification_email`."""
    return (
        sorted(spec["notification_emails"]) if "notification_emails" in spec else [spec["notification_email"]]
    )


def fallback_recipients(spec, config):
    """Fallback-topic recipients: `fallback_email` if the runtime config sets one, else the report recipients."""
    return [config["fallback_email"]] if "fallback_email" in config else recipients(spec)


def topic_arn(spec, topic):
    return f"arn:aws:sns:{spec['monitor_region']}:{spec['account_id']}:{name(spec, topic)}"


def alarm_descriptors(spec):
    output = []
    for instance in spec["instances"]:
        iid = instance["id"]
        dimensions = {"InstanceId": iid}
        definitions = [
            ("status", "AWS/EC2", "StatusCheckFailed", "Maximum", dimensions, 0, "GreaterThanThreshold", 60)
        ]
        if instance["resource_alarms"]:
            definitions += [
                ("cpu", "AWS/EC2", "CPUUtilization", "Average", dimensions, 90, "GreaterThanThreshold", 300),
                (
                    "memory",
                    "CWAgent",
                    "mem_used_percent",
                    "Average",
                    dimensions,
                    90,
                    "GreaterThanThreshold",
                    300,
                ),
                (
                    "disk",
                    "CWAgent",
                    "disk_used_percent",
                    "Average",
                    {**dimensions, "path": instance["disk_path"]},
                    85,
                    "GreaterThanThreshold",
                    300,
                ),
            ]
        if instance["process_exe"]:
            definitions.append(
                (
                    "process",
                    "CWAgent",
                    "procstat_lookup_pid_count",
                    "Minimum",
                    {**dimensions, "exe": instance["process_exe"], "pid_finder": "native"},
                    1,
                    "LessThanThreshold",
                    60,
                )
            )
        if instance["nginx_alarm"]:
            definitions.append(
                (
                    "nginx",
                    f"{spec['project']}/{spec['environment']}/Nginx",
                    f"nginx-upstream-errors-{iid}",
                    "Sum",
                    {},
                    5,
                    "GreaterThanOrEqualToThreshold",
                    60,
                )
            )
        if "observability" in spec and instance["nginx_alarm"]:
            definitions = [d for d in definitions if d[0] != "nginx"] + [
                (
                    "nginx-requests",
                    f"{spec['project']}/{spec['environment']}/Nginx",
                    f"nginx-failed-requests-{iid}",
                    "Sum",
                    {},
                    5,
                    "GreaterThanOrEqualToThreshold",
                    60,
                ),
                (
                    "nginx-diagnostics",
                    f"{spec['project']}/{spec['environment']}/Nginx",
                    f"nginx-diagnostic-events-{iid}",
                    "Sum",
                    {},
                    5,
                    "GreaterThanOrEqualToThreshold",
                    60,
                ),
            ]
        for suffix, namespace, metric, statistic, dims, threshold, comparison, period in definitions:
            output.append(
                {
                    "id": f"{iid}-{suffix}",
                    "instance_id": iid,
                    "namespace": namespace,
                    "metric_name": metric,
                    "statistic": statistic,
                    "dimensions": dims,
                    "threshold": threshold,
                    "comparison": comparison,
                    "period": period,
                    "alarm_name": name(spec, f"{iid}-{suffix}"),
                }
            )
    for service in ecs_services(spec):
        sid = service["id"]
        cluster = {"ClusterName": service["cluster"], "ServiceName": service["service"]}
        definitions = []
        if service.get("resource_alarms", True):
            definitions += [
                (
                    "cpu",
                    "AWS/ECS",
                    "CPUUtilization",
                    "Average",
                    cluster,
                    90,
                    "GreaterThanThreshold",
                    300,
                    "missing",
                ),
                (
                    "memory",
                    "AWS/ECS",
                    "MemoryUtilization",
                    "Average",
                    cluster,
                    90,
                    "GreaterThanThreshold",
                    300,
                    "missing",
                ),
            ]
        if service.get("container_insights"):
            definitions.append(
                (
                    "running",
                    "ECS/ContainerInsights",
                    "RunningTaskCount",
                    "Minimum",
                    cluster,
                    1,
                    "LessThanThreshold",
                    60,
                    "missing",
                )
            )
        if service["load_balancer"]:
            balancer = alb_dimensions(service["load_balancer"])
            definitions += [
                (
                    "alb5xx",
                    "AWS/ApplicationELB",
                    "HTTPCode_Target_5XX_Count",
                    "Sum",
                    balancer,
                    5,
                    "GreaterThanOrEqualToThreshold",
                    60,
                    "notBreaching",
                ),
                (
                    "unhealthy",
                    "AWS/ApplicationELB",
                    "UnHealthyHostCount",
                    "Maximum",
                    balancer,
                    0,
                    "GreaterThanThreshold",
                    60,
                    "notBreaching",
                ),
            ]
        for suffix, namespace, metric, statistic, dims, threshold, comparison, period, missing in definitions:
            output.append(
                {
                    "id": f"{sid}-{suffix}",
                    "instance_id": sid,
                    "namespace": namespace,
                    "metric_name": metric,
                    "statistic": statistic,
                    "dimensions": dims,
                    "threshold": threshold,
                    "comparison": comparison,
                    "period": period,
                    "missing_data": missing,
                    "owner": None,
                    # The ALB reports a 5xx count only when it is above zero, so a healthy service has no such metric.
                    "sparse": suffix == "alb5xx",
                    "alarm_name": name(spec, f"{sid}-{suffix}"),
                }
            )
    if "observability" in spec:
        settings = spec["observability"]
        for service in settings["services"]:
            for route in service["routes"] + [{"id": "telemetry"}]:
                telemetry = route["id"] == "telemetry"
                output.append(
                    {
                        "id": f"{service['instance_id']}-{service['id']}-{route['id']}",
                        "instance_id": service["instance_id"],
                        "namespace": f"{prefix(spec).replace('-', '/', 1)}/Health",
                        "metric_name": "TelemetryFresh" if telemetry else "Availability",
                        "statistic": "Minimum",
                        "dimensions": {"Service": service["id"], "Route": route["id"]},
                        "threshold": 1,
                        "comparison": "LessThanThreshold",
                        "period": settings["interval_minutes"] * 60,
                        "missing_data": "breaching",
                        "owner": service["owner"],
                        "alarm_name": name(spec, f"{service['instance_id']}-{service['id']}-{route['id']}"),
                    }
                )
    return output


def metric_catalog(spec):
    return [
        {
            key: item[key]
            for key in ("id", "instance_id", "namespace", "metric_name", "statistic", "dimensions")
        }
        for item in alarm_descriptors(spec)
    ]


def log_groups(spec):
    return sorted(
        {
            f"{log_prefix(spec)}/{item['id']}/{suffix}"
            for item in spec["instances"]
            for suffix in set(item["log_groups"])
            | ({"nginx-access", "nginx-error"} if item["nginx_alarm"] else set())
        }
    )


def existing_log_groups(spec):
    """{target ID: {group: "instance" or "all"}} for groups Tracaus reads and never creates."""
    found = {
        item["id"]: {g["name"]: g.get("streams", "instance") for g in item["existing_log_groups"]}
        for item in spec["instances"]
        if item.get("existing_log_groups")
    }
    # An awslogs stream is named prefix/container/task, never after the target, so ECS groups are read whole.
    found.update(
        {
            item["id"]: {g["name"]: "all" for g in item["existing_log_groups"]}
            for item in ecs_services(spec)
            if item.get("existing_log_groups")
        }
    )
    return found


def readable_log_groups(spec):
    """Every group the log tool may query: the ones Tracaus creates plus the existing ones."""
    return sorted(
        set(log_groups(spec)) | {g for groups in existing_log_groups(spec).values() for g in groups}
    )


def shipped(spec, instance):
    """Group names the generated agent file already ships (nginx, heartbeat), so log_files cannot repeat them."""
    nginx = {"nginx-access", "nginx-error"} if instance["nginx_alarm"] else set()
    services = spec.get("observability", {}).get("services", [])
    return nginx | {s["heartbeat_log_group"] for s in services if s["instance_id"] == instance["id"]}


def log_file(spec, instance, entry):
    """One agent collect_list item. Only a group Tracaus creates gets its retention set by the agent."""
    item = {"file_path": entry["file"], "log_stream_name": instance["id"]}
    if entry["group"] in instance["log_groups"]:
        item["log_group_name"] = f"{log_prefix(spec)}/{instance['id']}/{entry['group']}"
        item["retention_in_days"] = spec["log_retention_days"]
    else:
        item["log_group_name"] = entry["group"]
    return item


def cwagent(spec, instance):
    metrics = {
        "mem": {"measurement": ["mem_used_percent"]},
        "swap": {"measurement": ["swap_used_percent"]},
        "disk": {"measurement": ["used_percent"], "resources": [instance["disk_path"]], "drop_device": True},
    }
    if instance["process_exe"]:
        metrics["procstat"] = [{"exe": instance["process_exe"], "measurement": ["pid_count"]}]
    return {
        "agent": {"metrics_collection_interval": 60},
        "metrics": {
            "namespace": "CWAgent",
            "append_dimensions": {"InstanceId": "${aws:InstanceId}"},
            "aggregation_dimensions": [["InstanceId"], ["InstanceId", "path"]],
            "metrics_collected": metrics,
        },
        "logs": {
            "logs_collected": {
                "files": {
                    "collect_list": [
                        {
                            "file_path": f"/var/log/nginx/{kind}.log",
                            "log_group_name": f"{log_prefix(spec)}/{instance['id']}/nginx-{kind}",
                            "log_stream_name": instance["id"],
                            "retention_in_days": spec["log_retention_days"],
                        }
                        for kind in ("access", "error")
                        if instance["nginx_alarm"]
                    ]
                    + [log_file(spec, instance, entry) for entry in instance.get("log_files", [])]
                    + (
                        [
                            {
                                "file_path": "/var/log/tracaus-collector-heartbeat.log",
                                "log_group_name": f"{log_prefix(spec)}/{instance['id']}/{next(s['heartbeat_log_group'] for s in spec['observability']['services'] if s['instance_id'] == instance['id'])}",
                                "log_stream_name": instance["id"],
                                "retention_in_days": spec["log_retention_days"],
                            }
                        ]
                        if "observability" in spec
                        else []
                    )
                }
            }
        },
    }
