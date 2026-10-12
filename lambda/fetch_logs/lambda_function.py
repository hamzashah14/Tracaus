"""Bedrock Agent tool: list an instance's log groups, or search one around an incident."""

import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from tracaus import cursor
from tracaus.metrics import catalog
from tracaus.time import parse_utc
from tracaus.transport import bounded_envelope, error_result, fits, parameters

MONITOR_REGION = os.environ.get("MONITOR_REGION") or os.environ.get("AWS_REGION")
LOG_GROUP_PREFIX = (os.environ.get("LOG_GROUP_PREFIX") or "/aiops").rstrip("/")
BOTO_CONFIG = Config(connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1})

# Conservative application budget for the complete Bedrock response envelope.
MAX_BODY_BYTES = 20_000
MAX_MESSAGE_CHARS = 500
DEFAULT_LINES, MAX_LINES = 20, 50
DEFAULT_WINDOW_MINUTES, MAX_WINDOW_MINUTES = 30, 180
DEFAULT_HOURS_BACK, MAX_HOURS_BACK = 1, 24
MAX_ACTIVITY_BINS = 60
MIN_GAP_MINUTES = 2
# CloudWatch Logs ingestion can lag a few minutes, so zero-line bins right up
# to "now" aren't evidence of silence.
INGESTION_LAG = timedelta(minutes=3)
TIME_FMT = "%Y-%m-%d %H:%M:%S"
BIN_FMT = "%Y-%m-%d %H:%M"

INSTANCE_ID_RE = re.compile(r"^i-(?:[0-9a-f]{8}|[0-9a-f]{17})$")
LOG_GROUP_RE = re.compile(r"^[A-Za-z0-9_\-./#]+$")
_REGEX_SPECIAL = set(".^$*+?()[]{}|\\/")


class BadInput(ValueError):
    pass


class QueryFailed(RuntimeError):
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


def parse_instance_id(raw):
    if not raw:
        return None
    raw = str(raw).strip()
    if not INSTANCE_ID_RE.match(raw):
        raise BadInput(
            f"instance_id {raw!r} is not an EC2 instance ID (i- followed by 8 or 17 hex characters)."
        )
    return raw


def parse_log_group(raw, existing=()):
    if not raw:
        return None
    raw = str(raw).strip()
    if not LOG_GROUP_RE.match(raw) or not (raw.startswith(LOG_GROUP_PREFIX + "/") or raw in existing):
        raise BadInput(
            f"log_group_name must be a log group under {LOG_GROUP_PREFIX}/ or one of this instance's "
            "existing log groups. Call fetch_logs with only instance_id to list the valid ones."
        )
    return raw


def parse_time(raw):
    if not raw:
        return None
    try:
        return parse_utc(str(raw))
    except ValueError:
        raise BadInput("time_string must be formatted as YYYY-MM-DD HH:MM:SS (UTC).")


def _escape(text):
    return "".join("\\" + c if c in _REGEX_SPECIAL else c for c in text)


def build_text_filter(text):
    """Insights clause for the agent's search text: plain text, "a|b" alternatives,
    or CloudWatch-style ?a ?"b c" OR-terms, always case-insensitive. Empty = all lines."""
    text = (text or "").strip()
    if not text:
        return ""
    if "|" in text:
        alts = text.split("|")
    elif text.startswith("?"):
        alts = [a or b for a, b in re.findall(r'\?"([^"]*)"|\?(\S+)', text)]
    else:
        alts = [text]
    alts = [a.strip().strip('"').strip() for a in alts]
    alts = [a for a in alts if a]
    if not alts:
        return ""
    return f"| filter @message like /(?i)({'|'.join(_escape(a) for a in alts)})/ "


def stream_clause(log_group, instance_id, all_streams=False):
    # A group under <prefix>/<instance-id>/ already belongs to that instance, and
    # Docker's awslogs driver names its streams after container IDs, not instance
    # IDs — filtering on the stream there would silently match nothing. An existing
    # group declared with streams "all" belongs to one instance and is read whole.
    if not instance_id or all_streams or log_group.startswith(f"{LOG_GROUP_PREFIX}/{instance_id}/"):
        return ""
    return f"| filter @logStream = '{instance_id}' "


def lines_query(where, order, limit):
    return f"fields @timestamp, @message {where}| sort @timestamp {order} | limit {limit}"


def activity_query(stream, bin_minutes):
    return f"fields @timestamp {stream}| stats count(*) as lines by bin({bin_minutes}m)"


def _row_to_dict(row):
    return {field["field"]: field["value"] for field in row}


def discover_log_groups(client, instance_id, continuation=None, event=None):
    prefix = f"{LOG_GROUP_PREFIX}/{instance_id}/"
    bound_scope = cursor.scope(instance_id, LOG_GROUP_PREFIX, MONITOR_REGION)
    kwargs = {"logGroupNamePrefix": prefix, "limit": 20}
    if continuation:
        kwargs["nextToken"] = cursor.decode(continuation, bound_scope)
    response = client.describe_log_groups(**kwargs)
    names = [group["logGroupName"] for group in response.get("logGroups", [])]
    allowed_groups = configured_log_scope()
    if allowed_groups is not None:
        names = [group for group in names if group in allowed_groups]
    if not continuation:
        names += sorted(configured_existing_groups().get(instance_id, {}))
    token = response.get("nextToken")
    result = {
        "status": "log_groups_found" if names or token else "no_log_groups_found",
        "instance_id": instance_id,
        "log_groups": names,
        "complete": not bool(token),
        "truncated": False,
    }
    hints = [
        {"metric_id": m["id"], "metric_name": m["metric_name"]}
        for m in catalog()
        if m["instance_id"] == instance_id
    ]
    result["metric_catalog_complete"] = len(json.dumps(hints).encode()) <= 4000
    result["metric_catalog"] = hints if result["metric_catalog_complete"] else []
    if not fits(event or {}, result):
        result["metric_catalog"] = []
        result["metric_catalog_complete"] = False
    if token:
        result["next_token"] = cursor.encode(token, bound_scope)
    if not names and not token:
        result["message"] = "No log groups found in this instance scope. Check telemetry configuration."
    if not fits(event or {}, result):
        raise BadInput("Discovery page exceeds the response budget; no groups were silently omitted.")
    return result


def run_queries(client, log_group, queries, deadline):
    """Start every query at once, then poll until all finish or the deadline passes.
    queries: {name: (query_string, start_dt, end_dt)} -> {name: [row dict, ...]}"""
    ids = {}

    def check_budget():
        from tracaus.tool_deadline import check

        try:
            check(deadline)
        except ValueError:
            raise QueryFailed("Log query deadline reached") from None

    try:
        for name, (query, start, end) in queries.items():
            check_budget()
            ids[name] = client.start_query(
                logGroupName=log_group,
                startTime=int(start.timestamp()),
                endTime=int(end.timestamp()),
                queryString=query,
            )["queryId"]
        results, pending = {}, dict(ids)
        while pending:
            for name, query_id in list(pending.items()):
                check_budget()
                response = client.get_query_results(queryId=query_id)
                status = response["status"]
                if status == "Complete":
                    results[name] = [_row_to_dict(row) for row in response.get("results", [])]
                    del pending[name]
                elif status in ("Failed", "Cancelled", "Timeout", "Unknown"):
                    raise QueryFailed(f"Logs Insights query {status.lower()}.")
            if pending:
                if time.monotonic() > deadline:
                    raise QueryFailed("Logs Insights didn't finish in time — try a smaller window_minutes.")
                time.sleep(1)
        return results
    except Exception:
        for query_id in ids.values():
            try:
                client.stop_query(queryId=query_id)
            except Exception:
                pass
        raise


def summarize_activity(rows, start, end, bin_minutes, now):
    """Log lines per bin across the whole window, and runs of empty bins
    (a frozen process usually shows up as silence, not as an error line)."""
    counts = {}
    for row in rows:
        bucket = next((v for k, v in row.items() if k.startswith("bin(")), None)
        if bucket:
            counts[bucket[:16]] = counts.get(bucket[:16], 0) + int(float(row.get("lines", 0)))

    step = bin_minutes * 60
    bins, t = [], int(start.timestamp()) // step * step
    while t < end.timestamp():
        key = datetime.fromtimestamp(t, timezone.utc).strftime(BIN_FMT)
        bins.append((key, counts.get(key, 0)))
        t += step

    gaps, run = [], []
    for key, n in bins + [(None, 1)]:
        if key is not None and n == 0:
            run.append(key)
            continue
        if run and len(run) * bin_minutes >= MIN_GAP_MINUTES:
            gap_end = datetime.strptime(run[-1], BIN_FMT).replace(tzinfo=timezone.utc) + timedelta(
                minutes=bin_minutes
            )
            gap = {"from": run[0], "to": gap_end.strftime(BIN_FMT), "minutes": len(run) * bin_minutes}
            if gap_end >= now - INGESTION_LAG:
                gap["note"] = "reaches the present — may just be log ingestion delay, not silence"
            gaps.append(gap)
        run = []

    return {
        "bin_minutes": bin_minutes,
        "total_lines": sum(n for _, n in bins),
        "silent_gaps": gaps,
        "lines_per_bin": " ".join(f"{k[11:]}={n}" for k, n in bins),
    }


def _format_lines(rows):
    lines = []
    for row in rows:
        message = (row.get("@message") or "").strip()
        if len(message) > MAX_MESSAGE_CHARS:
            message = message[:MAX_MESSAGE_CHARS] + " …[truncated]"
        lines.append({"timestamp": (row.get("@timestamp") or "")[:19], "message": message})
    return lines


def configured_log_scope():
    path = os.getenv("LOG_SCOPE_FILE")
    if not path:
        return None  # Development only; deployed tools require a packaged scope.
    value = json.loads(Path(path).read_text())
    empty_ok = bool(os.getenv("EXISTING_LOG_GROUPS_FILE"))  # an instance may have only existing groups
    if (
        not isinstance(value, list)
        or (not value and not empty_ok)
        or any(not isinstance(i, str) for i in value)
    ):
        raise BadInput("Deployment log scope is invalid.")
    return set(value)


def configured_existing_groups():
    """{instance ID: {existing log group: "instance" or "all"}}; empty unless the deployment declares some."""
    path = os.getenv("EXISTING_LOG_GROUPS_FILE")
    if not path:
        return {}
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict) or any(
        not isinstance(groups, dict) or not set(groups.values()) <= {"instance", "all"}
        for groups in value.values()
    ):
        raise BadInput("Deployment existing log groups are invalid.")
    return value


def search(params, deadline, event=None):
    instance_id = parse_instance_id(params.get("instance_id"))
    existing = configured_existing_groups().get(instance_id, {})
    log_group = parse_log_group(params.get("log_group_name"), existing)
    scope = configured_log_scope()
    if scope is not None:
        if instance_id not in os.getenv("ALLOWED_INSTANCE_IDS", "").split(","):
            raise BadInput("Instance is outside the deployment inventory.")
        if log_group and log_group not in existing:
            if log_group not in scope or not log_group.startswith(f"{LOG_GROUP_PREFIX}/{instance_id}/"):
                raise BadInput("Log group is outside this instance's authorized inventory.")

    if not log_group:
        if not instance_id:
            raise BadInput(
                "Pass log_group_name, or instance_id on its own to list that instance's log groups."
            )
        client = boto3.client("logs", region_name=MONITOR_REGION, config=BOTO_CONFIG)
        return discover_log_groups(client, instance_id, params.get("next_token"), event)

    if params.get("next_token"):
        raise BadInput("next_token is only valid for discovery.")
    filter_text = params.get("filter_text", params.get("filter_pattern", ""))
    if not isinstance(filter_text, str) or len(filter_text.encode("utf-8")) > 1024:
        raise BadInput("filter_text must contain at most 1024 UTF-8 bytes.")
    text_clause = build_text_filter(filter_text)
    limit = _int_param(params, "lines", DEFAULT_LINES, 1, MAX_LINES)
    anchor = parse_time(params.get("time_string"))
    now = datetime.now(timezone.utc)
    stream = stream_clause(log_group, instance_id, existing.get(log_group) == "all")
    where = stream + text_clause

    if anchor:
        if anchor > now:
            raise BadInput(
                f"time_string {anchor.strftime(TIME_FMT)} is in the future (now is {now.strftime(TIME_FMT)} UTC)."
            )
        window = _int_param(params, "window_minutes", DEFAULT_WINDOW_MINUTES, 1, MAX_WINDOW_MINUTES)
        start, end = anchor - timedelta(minutes=window), min(anchor + timedelta(minutes=window), now)
        queries = {"before": (lines_query(where, "desc", limit), start, anchor)}
        if end > anchor:
            queries["after"] = (lines_query(where, "asc", limit), anchor, end)
    else:
        hours = _int_param(params, "hours_back", DEFAULT_HOURS_BACK, 1, MAX_HOURS_BACK)
        start, end = now - timedelta(hours=hours), now
        queries = {"recent": (lines_query(where, "desc", limit), start, end)}

    bin_minutes = max(1, math.ceil((end - start).total_seconds() / 60 / MAX_ACTIVITY_BINS))
    queries["activity"] = (activity_query(stream, bin_minutes), start, end)
    client = boto3.client("logs", region_name=MONITOR_REGION, config=BOTO_CONFIG)
    rows = run_queries(client, log_group, queries, deadline)

    result = {
        "status": "ok",
        "complete": True,
        "truncated": False,
        "log_group": log_group,
        "instance_id": instance_id,
        "filter_text": filter_text or None,
        "window_start": start.strftime(TIME_FMT),
        "window_end": end.strftime(TIME_FMT),
        "activity_all_lines": summarize_activity(rows["activity"], start, end, bin_minutes, now),
    }
    if anchor:
        result["incident_time"] = anchor.strftime(TIME_FMT)
        result["lines_before"] = _format_lines(reversed(rows["before"]))
        result["lines_after"] = _format_lines(rows.get("after", []))
        found = result["lines_before"] or result["lines_after"]
    else:
        result["lines"] = _format_lines(reversed(rows["recent"]))
        found = result["lines"]
    if not found:
        result["status"] = "no_matching_lines"
        result["message"] = (
            "No lines matched in this window; activity_all_lines still shows whether anything was logged."
        )
    return result


def lambda_handler(event, context):
    status = 200
    try:
        params = parameters(event)
        from tracaus.tool_deadline import remaining

        budget_s = remaining(event, context)
        deadline = time.monotonic() + max(0, budget_s)
        result = search(params, deadline, event)
    except (BadInput, ValueError) as exc:
        result, status = error_result("INVALID_REQUEST", str(exc)), 400
    except QueryFailed:
        result, status = (
            error_result(
                "LOG_QUERY_INCOMPLETE", "Log queries did not complete. Narrow the requested window and retry."
            ),
            502,
        )
    except ClientError:
        result, status = (
            error_result(
                "LOG_ACCESS_FAILED",
                "CloudWatch could not return logs. Check access, group names and service availability.",
            ),
            502,
        )
    except Exception:
        result, status = (
            error_result(
                "LOG_QUERY_FAILED",
                "Log lookup failed. Check deployment configuration and service availability.",
            ),
            502,
        )
    return bounded_envelope(event, result, status)
