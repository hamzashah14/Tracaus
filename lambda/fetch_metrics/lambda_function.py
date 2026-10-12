"""Bedrock Agent tool: one CloudWatch metric for one EC2 instance or ECS service, around an incident."""

import math
import os
import re
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from tracaus.metrics import resolve
from tracaus.time import parse_utc
from tracaus.transport import bounded_envelope, error_result, parameters

MONITOR_REGION = os.environ.get("MONITOR_REGION") or os.environ.get("AWS_REGION")
BOTO_CONFIG = Config(connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1})


MAX_DATAPOINTS = 60
DEFAULT_WINDOW_MINUTES, MAX_WINDOW_MINUTES = 30, 180
DEFAULT_HOURS_BACK, MAX_HOURS_BACK = 1, 72
TIME_FMT = "%Y-%m-%d %H:%M:%S"

INSTANCE_ID_RE = re.compile(r"^(?:i-(?:[0-9a-f]{8}|[0-9a-f]{17})|ecs-[a-z0-9_]{1,24})$")


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


def resolve_window(params, now):
    raw_time = params.get("time_string")
    if raw_time:
        try:
            anchor = parse_utc(str(raw_time))
        except ValueError:
            raise BadInput("time_string must be formatted as YYYY-MM-DD HH:MM:SS (UTC).")
        if anchor > now:
            raise BadInput(f"time_string is in the future (now is {now.strftime(TIME_FMT)} UTC).")
        window = timedelta(
            minutes=_int_param(params, "window_minutes", DEFAULT_WINDOW_MINUTES, 1, MAX_WINDOW_MINUTES)
        )
        return anchor - window, min(anchor + window, now)
    hours = _int_param(params, "hours_back", DEFAULT_HOURS_BACK, 1, MAX_HOURS_BACK)
    return now - timedelta(hours=hours), now


def pick_period(start, end, requested_minutes, now):
    """Seconds per datapoint: at least what was asked, few enough points to keep the
    response small, and coarse enough for CloudWatch's retention on older data
    (1-minute points only exist for 15 days, 5-minute points for 63)."""
    period = max(
        60, requested_minutes * 60, math.ceil((end - start).total_seconds() / MAX_DATAPOINTS / 60) * 60
    )
    age = now - start
    if age > timedelta(days=63):
        return math.ceil(period / 3600) * 3600
    if age > timedelta(days=15):
        return math.ceil(period / 300) * 300
    return period


def fetch(params):
    try:
        descriptor = resolve(params)
    except ValueError as exc:
        raise BadInput(str(exc)) from exc
    instance_id, metric_name = descriptor["instance_id"], descriptor["metric_name"]
    namespace, statistic = descriptor["namespace"], descriptor["statistic"]
    path = next((d["Value"] for d in descriptor["dimensions"] if d["Name"] == "path"), None)

    now = datetime.now(timezone.utc)
    start, end = resolve_window(params, now)
    period = pick_period(start, end, _int_param(params, "period_minutes", 1, 1, 1440), now)

    dimensions = descriptor["dimensions"]

    client = boto3.client("cloudwatch", region_name=MONITOR_REGION, config=BOTO_CONFIG)
    response = client.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric_name,
        Dimensions=dimensions,
        StartTime=start,
        EndTime=end,
        Period=period,
        Statistics=[statistic],
        **({"Unit": descriptor["unit"]} if "unit" in descriptor else {}),
    )
    datapoints = sorted(response.get("Datapoints", []), key=lambda d: d["Timestamp"])
    base = {
        "metric": metric_name,
        "namespace": namespace,
        "instance_id": instance_id,
        "path": path,
        "statistic": statistic,
        "period_seconds": period,
        "descriptor": descriptor,
        "complete": True,
        "window_start": start.strftime(TIME_FMT),
        "window_end": end.strftime(TIME_FMT),
    }
    if not datapoints:
        if namespace == "CWAgent":
            hint = (
                "CWAgent metrics need the agent running on this instance with append_dimensions.InstanceId "
                "(and aggregation_dimensions for disk metrics)."
            )
        elif instance_id.startswith("ecs-"):
            hint = (
                "ECS publishes service metrics only while tasks run; Container Insights metrics need it "
                "turned on for the cluster, and ALB metrics need traffic."
            )
        else:
            hint = "The instance may have been stopped during this window."
        return {"status": "no_data", **base, "message": f"No datapoints in this window. {hint}"}

    values = [dp[statistic] for dp in datapoints]
    return {
        "status": "ok",
        **base,
        "unit": datapoints[0].get("Unit", "None"),
        "summary": {
            "first": round(values[0], 2),
            "last": round(values[-1], 2),
            "average": round(sum(values) / len(values), 2),
            "maximum": round(max(values), 2),
            "minimum": round(min(values), 2),
        },
        "datapoints": [
            {
                "timestamp": dp["Timestamp"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "value": round(dp[statistic], 2),
            }
            for dp in datapoints
        ],
    }


def lambda_handler(event, context):
    status = 200
    try:
        from tracaus.tool_deadline import remaining

        remaining(event, context)
        result = fetch(parameters(event))
    except (BadInput, ValueError) as exc:
        result, status = error_result("INVALID_REQUEST", str(exc)), 400
    except ClientError:
        result, status = (
            error_result(
                "METRIC_ACCESS_FAILED",
                "CloudWatch could not return this metric. Check access and service availability.",
            ),
            502,
        )
    except Exception:
        result, status = (
            error_result(
                "METRIC_FAILED",
                "Metric lookup failed. Check deployment configuration and service availability.",
            ),
            502,
        )
    return bounded_envelope(event, result, status)
