"""One timestamp contract: ISO-8601 offsets normalize to UTC; naive timestamps is UTC."""

import re
from datetime import datetime, timezone

TIME_FMT = "%Y-%m-%d %H:%M:%S"
STAMP = re.compile(r"^\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:?\d\d)?$")


def parse_utc(value):
    if not isinstance(value, str) or not STAMP.fullmatch(value.strip()):
        raise ValueError("Timestamp must be ISO-8601 or YYYY-MM-DD HH:MM:SS (UTC).")
    value = value.strip()
    if re.search(r"[+-](?:2[4-9]|[3-9]\d):?\d\d$|[+-]\d\d:?[6-9]\d$", value):
        raise ValueError("Timestamp offset is invalid.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def iso_utc(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
