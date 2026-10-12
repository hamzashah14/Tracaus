"""Absolute caller deadlines for tool invocations; calls without caller deadlines retain Lambda bounds."""

import math
import time


def remaining(event, context, default=25):
    seconds = context.get_remaining_time_in_millis() / 1000 - 5 if context else default
    if "runtime_deadline_epoch" in event:
        deadline = event["runtime_deadline_epoch"]
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("Invalid tool deadline")
        seconds = min(seconds, deadline - time.time(), 30)
    if seconds < 13:
        raise ValueError("Insufficient tool deadline")
    return seconds


def check(deadline):
    if deadline - time.monotonic() < 13:
        raise ValueError("Insufficient query deadline")
