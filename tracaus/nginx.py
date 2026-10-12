"""Extract safe evidence from the declared Nginx combined access format."""

import re
from datetime import datetime, timezone

FAILED_STATUSES = {500, 502, 503, 504}
LINE = re.compile(r'^\S+ \S+ \S+ \[([^\]]+)\] "[^"\r\n]+" (\d{3}) (?:\d+|-) "[^"\r\n]*" "[^"\r\n]*"$')


def access_evidence(line):
    if not isinstance(line, str) or len(line.encode()) > 4096:
        raise ValueError("Invalid access event")
    matched = LINE.fullmatch(line)
    if not matched:
        raise ValueError("Unsupported access format; validate the customer's actual format")
    timestamp = datetime.strptime(matched[1], "%d/%b/%Y:%H:%M:%S %z").astimezone(timezone.utc)
    status = int(matched[2])
    return {
        "occurred_at": timestamp.isoformat().replace("+00:00", "Z"),
        "status": status,
        "failed_request": status in FAILED_STATUSES,
    }
