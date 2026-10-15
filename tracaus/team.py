"""Team mode: a small allowlist file says who may use which instances.

Standard library only at import time. `tracaus/*.py` ships in every Lambda package and is imported with
`python -S`, so this module must not import streamlit or boto3 on import; the audit shipper imports boto3 only
when the UI process writes a line. Streamlit's OIDC login authenticates the person; this module only
authorizes the verified claims.
"""

import json
import os
import re
import stat
import threading
import time
import tomllib
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass

INSTANCE = re.compile(
    r"(?:i-(?:[0-9a-f]{8}|[0-9a-f]{17})|ecs-[a-z0-9_]{1,24})\Z"
)  # an EC2 instance or ECS service
ISSUER = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]*)?\Z")
SUBJECT = re.compile(r"\S{1,256}\Z")
ROLES = ("viewer", "investigator")
MAX_USERS = 100
MAX_INSTANCES = 100
MAX_FILE_BYTES = 256 * 1024
CLOCK_SKEW_SECONDS = 300

DENIED_MESSAGES = {
    "not_listed": "Your account is not on this team's access list. Ask the operator to add you.",
    "issuer": "Your sign-in came from an identity provider this workspace does not accept.",
    "expired": "Your sign-in is too old. Sign out and sign in again.",
    "mfa": "This workspace requires multi-factor sign-in, and your provider did not report it.",
    "claims": "Your sign-in did not include the details this workspace needs.",
}


class TeamError(ValueError):
    """The team file is missing, unreadable or invalid. Messages name fields, never values."""


class TeamDenied(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Access:
    sub: str
    role: str
    instances: frozenset


@dataclass(frozen=True)
class Roster:
    issuer: str
    require_mfa: bool
    session_seconds: int
    chat_per_hour: int
    users: dict

    def authorize(self, claims, now=None):
        """Return the person's access, or raise TeamDenied. Malformed claims are a denial, never an error."""
        now = time.time() if now is None else now
        if not isinstance(claims, Mapping):
            raise TeamDenied("claims")
        if claims.get("iss") != self.issuer:
            raise TeamDenied("issuer")
        sub = claims.get("sub")
        if not isinstance(sub, str) or sub not in self.users:
            raise TeamDenied("not_listed")
        stamp = claims.get("auth_time", claims.get("iat"))
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            raise TeamDenied("claims")
        try:
            age = now - stamp
        except OverflowError:
            raise TeamDenied("claims") from None
        if not -CLOCK_SKEW_SECONDS <= age <= self.session_seconds:
            raise TeamDenied("expired")
        if self.require_mfa:
            amr = claims.get("amr")
            if not (isinstance(amr, list) and "mfa" in amr):
                raise TeamDenied("mfa")
        return self.users[sub]


def _fail(field):
    raise TeamError(f"Team file field '{field}' is invalid")


def _only(table, keys, field):
    if not isinstance(table, dict):
        _fail(field)
    unknown = sorted(str(key) for key in set(table) - set(keys))
    if unknown:
        raise TeamError(f"Team file field '{field}' has unknown keys: {', '.join(unknown)}")


def _number(value, field, low, high):
    if type(value) is not int or not low <= value <= high:
        _fail(field)
    return value


def _parse(raw, deployed):
    _only(raw, {"issuer", "require_mfa", "session_hours", "limits", "users"}, "top level")
    issuer = raw.get("issuer")
    if not isinstance(issuer, str) or len(issuer) > 256 or not ISSUER.fullmatch(issuer):
        _fail("issuer")
    require_mfa = raw.get("require_mfa", True)
    if type(require_mfa) is not bool:
        _fail("require_mfa")
    hours = _number(raw.get("session_hours", 8), "session_hours", 1, 24)
    limits = raw.get("limits", {})
    _only(limits, {"chat_per_user_per_hour"}, "limits")
    per_hour = _number(limits.get("chat_per_user_per_hour", 20), "limits.chat_per_user_per_hour", 1, 1000)
    entries = raw.get("users")
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_USERS:
        _fail("users")
    users = {}
    for index, entry in enumerate(entries):
        field = f"users[{index}]"
        _only(entry, {"sub", "role", "instances"}, field)
        if set(entry) != {"sub", "role", "instances"}:
            _fail(field)
        sub, role, instances = entry["sub"], entry["role"], entry["instances"]
        if not isinstance(sub, str) or not SUBJECT.fullmatch(sub) or not sub.isprintable() or sub in users:
            _fail(f"{field}.sub")
        if role not in ROLES:
            _fail(f"{field}.role")
        if (
            not isinstance(instances, list)
            or not 1 <= len(instances) <= MAX_INSTANCES
            or len(set(map(str, instances))) != len(instances)
            or any(
                not isinstance(i, str) or not INSTANCE.fullmatch(i) or i not in deployed for i in instances
            )
        ):
            _fail(f"{field}.instances")
        users[sub] = Access(sub, role, frozenset(instances))
    return Roster(issuer, require_mfa, hours * 3600, per_hour, users)


_CACHE = {}


def load(path, allowed):
    """Return the roster for `path`, re-parsing only when the file or the deployed instances change."""
    deployed = frozenset(allowed)
    try:
        info = os.stat(path)
    except (OSError, TypeError, ValueError):
        raise TeamError("Team file is missing or unreadable") from None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES or info.st_mode & 0o022:
        raise TeamError("Team file must be a regular file under 256 KiB that others cannot write")
    # ponytail: mtime, size, and ctime; same-size edit within one ctime tick with preserved mtime will miss the reload.
    key = (info.st_mtime_ns, info.st_size, info.st_ctime_ns, deployed)
    cached = _CACHE.get(path)
    if cached and cached[0] == key:
        return cached[1]
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, ValueError, RecursionError):
        raise TeamError("Team file cannot be read as TOML") from None
    roster = _parse(raw, deployed)
    _CACHE[path] = (key, roster)
    return roster


_HITS = {}
_LOCK = threading.Lock()


def admit(sub, limit, now=None):
    """Count one request in a sliding one-hour window per subject; False when the limit is reached."""
    now = time.time() if now is None else now
    with _LOCK:
        hits = _HITS.setdefault(sub, deque())
        while hits and now - hits[0] >= 3600:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True


_SHIP = {"client": None, "stream": None, "failed": False}
_SHIP_LOCK = threading.Lock()


def ship(text):
    """Write one audit line to the CloudWatch Logs group named by AUDIT_LOG_GROUP. True when it was written."""
    group = os.getenv("AUDIT_LOG_GROUP")
    if not group:
        return False
    try:
        with _SHIP_LOCK:
            if _SHIP["client"] is None:
                import boto3
                from botocore.config import Config

                client = boto3.client(
                    "logs",
                    region_name=os.environ["MONITOR_REGION"],
                    config=Config(
                        connect_timeout=3,
                        read_timeout=5,
                        retries={"mode": "standard", "total_max_attempts": 2},
                    ),
                )
                stream = (
                    f"{time.strftime('%Y-%m-%d', time.gmtime())}-{uuid.uuid4().hex[:12]}"  # one per process
                )
                try:
                    client.create_log_stream(logGroupName=group, logStreamName=stream)
                except client.exceptions.ResourceAlreadyExistsException:
                    pass
                _SHIP.update(client=client, stream=stream)
        _SHIP["client"].put_log_events(
            logGroupName=group,
            logStreamName=_SHIP["stream"],
            logEvents=[{"timestamp": int(time.time() * 1000), "message": text}],
        )
        return True
    except Exception:
        return False  # the line is still on standard output; the UI tells the operator


def audit_failed():
    """True when the last audit line did not reach CloudWatch Logs (or no group is configured)."""
    return _SHIP["failed"]


def audit(sub, role, instance, action, outcome, *, instance_count=None, tokens=None):
    """One JSON line on standard output and in CloudWatch Logs. Callers pass fixed vocabulary only, never prompt
    or log text."""
    line = {
        "ts": int(time.time()),
        "event": "tracaus.audit",
        "sub": sub,
        "role": role,
        "instance": instance,
        "action": action,
        "outcome": outcome,
    }
    if instance_count is not None:
        line["instance_count"] = instance_count
    if tokens:
        filtered = {k: v for k, v in tokens.items() if k in ("input", "output") and type(v) is int}
        if filtered:
            line["tokens"] = filtered
    text = json.dumps(line, separators=(",", ":"))
    print(text, flush=True)
    _SHIP["failed"] = not ship(text)
