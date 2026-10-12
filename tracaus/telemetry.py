"""Allowlisted structured events/EMF; correlation IDs are logs, never metric dimensions."""

import json
import math
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar

_correlation = ContextVar("tracaus_correlation", default=(None, None))


@contextmanager
def correlate(incident_id, fence):
    token = _correlation.set((incident_id, fence))
    try:
        yield
    finally:
        _correlation.reset(token)


METRICS = {
    "Accepted",
    "Duplicate",
    "Suppressed",
    "Ignored",
    "Failure",
    "QueueDelaySeconds",
    "Attempt",
    "ToolFailure",
    "ToolNoData",
    "Deadline",
    "InputTokens",
    "OutputTokens",
    "ModelCalls",
    "ReportPersisted",
    "NotificationAccepted",
    "SweepPending",
    "OldestOutboxSeconds",
    "RecipientReceived",
    "Heartbeat",
    "EmailReceiptAgeSeconds",
}
COMPONENTS = {
    "ingress",
    "dispatch",
    "work",
    "initial",
    "report",
    "reconcile",
    "model",
    "tool",
    "observer",
    "canary",
    "receipt",
}


def emit(component, outcome, *, incident_id=None, fence=None, metrics=None):
    if component not in COMPONENTS or not re.fullmatch(r"[A-Z_]{1,64}", outcome):
        raise ValueError("Unknown telemetry component/outcome")
    inherited_id, inherited_fence = _correlation.get()
    incident_id = incident_id if incident_id is not None else inherited_id
    fence = fence if fence is not None else inherited_fence
    metrics = metrics or {}
    if any(
        k not in METRICS or type(v) not in (int, float) or not math.isfinite(v) or v < 0
        for k, v in metrics.items()
    ):
        raise ValueError("Unknown or invalid telemetry metric")
    event = {"Component": component, "outcome": outcome, **metrics}
    if isinstance(incident_id, str) and re.fullmatch(r"[0-9a-f]{32}", incident_id):
        event["incident_id"] = incident_id
    if type(fence) is int and fence >= 0:
        event["fence"] = fence
    namespace = os.getenv("OBS_NAMESPACE")
    if namespace and metrics:
        if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/Pipeline", namespace):
            raise ValueError("Invalid telemetry namespace")
        event["_aws"] = {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [["Component"]],
                    "Metrics": [
                        {"Name": k, "Unit": "Seconds" if k.endswith("Seconds") else "Count"} for k in metrics
                    ],
                }
            ],
        }
    print(json.dumps(event, separators=(",", ":"), allow_nan=False))
