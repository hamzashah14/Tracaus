"""Versioned incident identity and source validation; no network calls."""

import hashlib
import json
import re
from datetime import datetime, timezone

from tracaus.time import iso_utc, parse_utc

MAX_SOURCE_BYTES = 128 * 1024
INSTANCE = re.compile(r"i-[0-9a-f]{17}\Z")


class InvalidEvent(ValueError):
    pass


def event_identity(source, native_id, *transition):
    value = json.dumps([source, native_id, *transition], separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_sns(
    raw, expected_topic, account, region, allowed, alarm_prefix, *, canary_topic=None, track_recovery=False
):
    """The SQS body is an SNS envelope. Keep original source bytes in the private event row."""
    if not isinstance(raw, str) or len(raw.encode()) > MAX_SOURCE_BYTES:
        raise InvalidEvent("Source envelope missing or too large")
    try:
        envelope = json.loads(raw)
        source = json.loads(envelope["Message"])
    except (TypeError, ValueError, KeyError) as exc:
        raise InvalidEvent("Invalid SNS source envelope") from exc
    if not isinstance(envelope, dict) or not isinstance(source, dict):
        raise InvalidEvent("Source message must be an object")
    if (
        envelope.get("TopicArn") not in ({expected_topic, canary_topic} if canary_topic else {expected_topic})
        or envelope.get("Type") != "Notification"
    ):
        raise InvalidEvent("Unexpected SNS source")
    if canary_topic and envelope.get("TopicArn") == canary_topic:
        if set(source) != {"source", "account", "region", "instance_id", "slot", "time"} or (
            source["source"] != "tracaus.canary"
            or source["account"] != account
            or source["region"] != region
            or type(source["slot"]) is not int
            or source["slot"] < 0
        ):
            raise InvalidEvent("Unexpected synthetic canary source")
        iid, transition, native = (
            source["instance_id"],
            source["time"],
            canary_topic + ":" + str(source["slot"]),
        )
        kind, state, actionable = "canary", "TEST", True
    elif envelope.get("TopicArn") != expected_topic:
        raise InvalidEvent("Unexpected SNS source")
    elif source.get("AlarmName"):
        alarm_arn = source.get("AlarmArn", "")
        arn_prefix = f"arn:aws:cloudwatch:{region}:{account}:alarm:"
        name = source["AlarmName"]
        if alarm_arn != arn_prefix + name or not name.startswith(alarm_prefix):
            raise InvalidEvent("Alarm source is outside this environment")
        match = re.fullmatch(re.escape(alarm_prefix) + r"(i-[0-9a-f]{17})-.+", name)
        if not match:
            raise InvalidEvent("Alarm does not identify one inventory instance")
        iid = match.group(1)
        dimensions = (source.get("Trigger") or {}).get("Dimensions", [])
        for item in dimensions:
            if item.get("name") == "InstanceId" and item.get("value") != iid:
                raise InvalidEvent("Alarm name and metric instance disagree")
        transition = source.get("StateChangeTime")
        state = source.get("NewStateValue")
        native = alarm_arn
        kind = "alarm"
        actionable = state == "ALARM"
    elif source.get("detail-type") == "EC2 Instance State-change Notification":
        native = source.get("id", "")
        if (
            source.get("account") != account
            or source.get("region") != region
            or source.get("source") != "aws.ec2"
            or not isinstance(native, str)
            or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", native)
        ):
            raise InvalidEvent("Unexpected EventBridge identity")
        detail = source.get("detail") or {}
        iid = detail.get("instance-id")
        state = detail.get("state")
        if state not in {"pending", "running", "stopping", "stopped", "shutting-down", "terminated"}:
            raise InvalidEvent("Unknown EC2 transition")
        transition = source.get("time")
        kind = "ec2"
        actionable = state in {"stopped", "terminated"}
    else:
        raise InvalidEvent("Unsupported source event")
    if iid not in allowed or not INSTANCE.fullmatch(iid):
        raise InvalidEvent("Source instance is outside deployment inventory")
    try:
        occurred_at = iso_utc(parse_utc(transition))
        if kind == "canary" and parse_utc(transition).timestamp() != source["slot"]:
            raise ValueError("Canary slot/time mismatch")
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("Source transition time is invalid") from exc
    if kind == "alarm" and state not in {"ALARM", "OK", "INSUFFICIENT_DATA"}:
        raise InvalidEvent("Unknown alarm transition")
    event_id = event_identity(kind, native, occurred_at, state)
    # Every transition gets its own incident. Related events share a grouping key,
    # but correlation never suppresses a new or recovery event.
    return {
        "schema_version": 1,
        "event_id": event_id,
        "incident_id": event_id[:32],
        "kind": kind,
        "native_id": native,
        "instance_id": iid,
        "state": state,
        "actionable": actionable,
        "occurred_at": occurred_at,
        "received_at": iso_utc(datetime.now(timezone.utc)),
        "correlation_key": f"{account}:{region}:{iid}",
        "source_account": account,
        "source_region": region,
        "source_topic": envelope["TopicArn"],
        "sns_message_id": envelope.get("MessageId"),
        "sns_timestamp": envelope.get("Timestamp"),
        "original_event": envelope["Message"],
        **({"canary_slot": source["slot"], "investigate": False} if kind == "canary" else {}),
        **({"track_recovery": True} if kind == "alarm" and track_recovery else {}),
    }
