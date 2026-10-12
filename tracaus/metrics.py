"""Resolve only deployment-authorized instances and exact metric descriptors."""

import json
import os
import re
from pathlib import Path

INSTANCE = re.compile(
    r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})|ecs-[a-z0-9_]{1,24}"
)  # an EC2 instance or an ECS service
STATISTICS = {"Average", "Maximum", "Minimum", "Sum", "SampleCount"}
EC2_METRICS = {
    "CPUUtilization",
    "CPUCreditBalance",
    "CPUCreditUsage",
    "CPUSurplusCreditBalance",
    "CPUSurplusCreditsCharged",
    "NetworkIn",
    "NetworkOut",
    "NetworkPacketsIn",
    "NetworkPacketsOut",
    "DiskReadBytes",
    "DiskWriteBytes",
    "DiskReadOps",
    "DiskWriteOps",
    "EBSReadOps",
    "EBSWriteOps",
    "EBSReadBytes",
    "EBSWriteBytes",
    "EBSIOBalance%",
    "EBSByteBalance%",
    "StatusCheckFailed",
    "StatusCheckFailed_Instance",
    "StatusCheckFailed_System",
    "StatusCheckFailed_AttachedEBS",
    "MetadataNoToken",
}
CW_METRICS = {"mem_used_percent", "swap_used_percent", "disk_used_percent", "procstat_lookup_pid_count"}
# ECS: what each service publishes by default, then Container Insights (opt-in) and the ALB (when one is in front).
ECS_METRICS = {"CPUUtilization": "Average", "MemoryUtilization": "Average"}
INSIGHTS_METRICS = {
    "RunningTaskCount": "Average",
    "DesiredTaskCount": "Average",
    "PendingTaskCount": "Average",
}
ALB_METRICS = {
    "HTTPCode_Target_5XX_Count": "Sum",
    "HTTPCode_Target_4XX_Count": "Sum",
    "RequestCount": "Sum",
    "TargetResponseTime": "Average",
    "UnHealthyHostCount": "Maximum",
    "HealthyHostCount": "Minimum",
}
UNITS = {
    "Seconds",
    "Microseconds",
    "Milliseconds",
    "Bytes",
    "Kilobytes",
    "Megabytes",
    "Gigabytes",
    "Terabytes",
    "Bits",
    "Kilobits",
    "Megabits",
    "Gigabits",
    "Terabits",
    "Percent",
    "Count",
    "Bytes/Second",
    "Kilobytes/Second",
    "Megabytes/Second",
    "Gigabytes/Second",
    "Terabytes/Second",
    "Bits/Second",
    "Kilobits/Second",
    "Megabits/Second",
    "Gigabits/Second",
    "Terabits/Second",
    "Count/Second",
    "None",
}


def validate_catalog(entries):
    if not isinstance(entries, list) or len(entries) > 100:
        raise ValueError("Metric catalog must contain at most 100 descriptors.")
    seen = set()
    required = {"id", "instance_id", "namespace", "metric_name", "statistic", "dimensions"}
    for item in entries:
        if not isinstance(item, dict) or not required <= item.keys() or item.keys() - required - {"unit"}:
            raise ValueError("Invalid metric descriptor fields.")
        if (
            not isinstance(item["id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item["id"])
            or item["id"] in seen
        ):
            raise ValueError("Metric descriptor IDs must be unique.")
        seen.add(item["id"])
        if not isinstance(item["instance_id"], str) or not INSTANCE.fullmatch(item["instance_id"]):
            raise ValueError("Metric descriptor must be bound to a valid instance.")
        for name, pattern in (
            ("namespace", r"[A-Za-z0-9_.\-/]{1,255}"),
            ("metric_name", r"[A-Za-z0-9_.%\-]{1,255}"),
        ):
            if not isinstance(item[name], str) or not re.fullmatch(pattern, item[name]):
                raise ValueError("Invalid metric namespace/name.")
        if (
            not isinstance(item["statistic"], str)
            or item["statistic"] not in STATISTICS
            or not isinstance(item.get("unit", "None"), str)
            or item.get("unit", "None") not in UNITS
        ):
            raise ValueError("Invalid statistic or unit.")
        dims = item["dimensions"]
        if not isinstance(dims, dict) or len(dims) > 30:
            raise ValueError("Metric dimensions must be a map with at most 30 entries.")
        for name, value in dims.items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.\-/]{1,255}", name):
                raise ValueError("Invalid dimension name.")
            if not isinstance(value, str) or not 1 <= len(value) <= 1024 or any(ord(c) < 32 for c in value):
                raise ValueError("Invalid dimension value.")
        if "InstanceId" in dims and dims["InstanceId"] != item["instance_id"]:
            raise ValueError("InstanceId dimension must match descriptor scope.")
    return entries


def catalog():
    path = Path(os.getenv("METRIC_CATALOG_FILE", "config/metric-catalog.json"))
    if path.stat().st_size > 262144:
        raise ValueError("Metric catalog exceeds the supported size.")
    return validate_catalog(json.loads(path.read_text()))


def ecs_targets():
    """{target ID: cluster, service and ALB dimensions} packaged with the tools; empty without ECS services."""
    path = Path(os.getenv("ECS_TARGETS_FILE", "config/ecs-targets.json"))
    return json.loads(path.read_text()) if path.exists() else {}


def ecs_builtin(iid, metric, namespace):
    """Namespace, dimensions and default statistic for a built-in ECS metric. Names never come from the model."""
    where = ecs_targets().get(iid)
    if where is None:
        raise ValueError("This ECS service has no packaged metric scope; redeploy the tools.")
    service = {"ClusterName": where["cluster"], "ServiceName": where["service"]}
    balancer = where["load_balancer"]
    for names, home, dims, enabled in (
        (ECS_METRICS, "AWS/ECS", service, True),
        (INSIGHTS_METRICS, "ECS/ContainerInsights", service, where["container_insights"]),
        (
            ALB_METRICS,
            "AWS/ApplicationELB",
            balancer and {"LoadBalancer": balancer["LoadBalancer"], "TargetGroup": balancer["TargetGroup"]},
            balancer,
        ),
    ):
        if metric in names and enabled and namespace in (None, "", home):
            return home, dict(dims), names[metric]
    raise ValueError(
        "Unsupported metric/namespace for this ECS service (Container Insights and ALB metrics exist only "
        "when configured). Use an allowlisted metric_id."
    )


def allowed_instance(value):
    if not isinstance(value, str) or not INSTANCE.fullmatch(value):
        raise ValueError("A valid instance_id (an EC2 instance or ECS service ID) is required.")
    allowed = {item.strip() for item in os.getenv("ALLOWED_INSTANCE_IDS", "").split(",") if item.strip()}
    if not allowed:
        raise ValueError("Metric scope is not configured; set ALLOWED_INSTANCE_IDS.")
    if value not in allowed:
        raise ValueError("This instance is not authorized by the deployment.")
    return value


def resolve(params):
    iid = allowed_instance(params.get("instance_id"))
    metric_id = params.get("metric_id")
    metric = params.get("metric_name") or "CPUUtilization"
    namespace = params.get("namespace")
    entries = catalog()
    match = next((item for item in entries if item["id"] == metric_id), None) if metric_id else None
    if metric_id and match is None:
        raise ValueError("metric_id is not in the configured catalog.")
    if match:
        if match["instance_id"] != iid:
            raise ValueError("Metric descriptor belongs to another instance.")
        for key in ("metric_name", "namespace", "statistic"):
            if params.get(key) and params[key] != match[key]:
                raise ValueError("Metric parameters conflict with the configured descriptor.")
        if params.get("path") and params["path"] != match["dimensions"].get("path"):
            raise ValueError("Path conflicts with the configured descriptor.")
        descriptor = dict(match)
    elif iid.startswith("ecs-"):
        namespace, dims, default = ecs_builtin(iid, metric, namespace)
        if params.get("path"):
            raise ValueError("path is only supported for a configured disk metric.")
        statistic = params.get("statistic") or default
        if statistic not in STATISTICS:
            raise ValueError("Unsupported statistic.")
        descriptor = {
            "id": "builtin",
            "instance_id": iid,
            "namespace": namespace,
            "metric_name": metric,
            "statistic": statistic,
            "dimensions": dims,
        }
    else:
        if metric in EC2_METRICS and namespace in (None, "", "AWS/EC2"):
            namespace, dims, default = "AWS/EC2", {"InstanceId": iid}, "Average"
        elif metric in CW_METRICS and namespace in (None, "", "CWAgent"):
            namespace, dims, default = "CWAgent", {"InstanceId": iid}, "Average"
            if metric == "disk_used_percent":
                path = params.get("path") or os.getenv("DISK_PATH", "/")
                if path != os.getenv("DISK_PATH", "/"):
                    raise ValueError(
                        "Path is not authorized; add an explicit metric_id descriptor for other mounts."
                    )
                dims["path"] = path
        elif metric == f"nginx-upstream-errors-{iid}" and namespace in (None, "", "AIOpsNginx"):
            namespace, dims, default = "AIOpsNginx", {}, "Sum"
        else:
            raise ValueError("Unsupported metric/namespace. Configure and use an allowlisted metric_id.")
        if params.get("path") and metric != "disk_used_percent":
            raise ValueError("path is only supported for a configured disk metric.")
        statistic = params.get("statistic") or default
        if statistic not in STATISTICS:
            raise ValueError("Unsupported statistic.")
        descriptor = {
            "id": "builtin",
            "instance_id": iid,
            "namespace": namespace,
            "metric_name": metric,
            "statistic": statistic,
            "dimensions": dims,
        }
    return {
        "metric_id": descriptor["id"],
        "instance_id": iid,
        "namespace": descriptor["namespace"],
        "metric_name": descriptor["metric_name"],
        "statistic": descriptor["statistic"],
        "dimensions": [
            {"Name": key, "Value": value} for key, value in sorted(descriptor["dimensions"].items())
        ],
        **({"unit": descriptor["unit"]} if descriptor.get("unit") else {}),
    }
