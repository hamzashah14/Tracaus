"""Resolve only deployment-authorized instances and exact metric descriptors."""

import json
import os
import re
from pathlib import Path

INSTANCE = re.compile(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})")
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


def allowed_instance(value):
    if not isinstance(value, str) or not INSTANCE.fullmatch(value):
        raise ValueError("A valid instance_id is required.")
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
