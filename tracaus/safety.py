"""Bounded secret/PII handling at every trust boundary; no regex completeness claim."""

import ipaddress
import json
import re
from urllib.parse import unquote

VERSION = "redaction-v1"
LIMIT = 128000
KEY = re.compile(
    r"(?i)^(?:password|passwd|secret|client_secret|api[_-]?key|access[_-]?token|refresh[_-]?token|token|access_ticket|session_ticket|session_token|session_id|aws_secret_access_key|secret_access_key|aws_session_token|aws_access_key_id|authorization|cookie|set-cookie|private[_-]?key|email|email_address|client_ip|phone_number|username|user_name)$"
)
PATTERNS = [
    (
        re.compile(r"-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----[\s\S]*?-----END (?:[A-Z ]*PRIVATE KEY)-----"),
        "[redacted-key]",
    ),
    (re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9+/=_\-.]+"), "[redacted-authorization]"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[redacted-aws-key]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[redacted-jwt]"),
    (
        re.compile(
            r"""(?ix)(?<![\w-])(["']?(?:password|passwd|secret|client_secret|api[_-]?key|access[_-]?token|refresh[_-]?token|token|access_ticket|session_ticket|session_token|session_id|aws_secret_access_key|secret_access_key|aws_session_token|aws_access_key_id|authorization|cookie|set-cookie|x-amz-signature|x-amz-credential)["']?\s*[:=]\s*)(?P<value>"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|\[redacted[^\]]*\]|[^\s&,;\}\]]+)"""
        ),
        None,
    ),
    (re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@"), r"\1[redacted]@"),
    (re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"), "[redacted-email]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[redacted-ip]"),
]


# Escapes of control characters and Unicode line breaks. They stay as typed (%0d%0a), so a logged URL cannot
# turn one message into two lines of evidence.
KEPT_ESCAPE = re.compile(r"(?i)%(?:0[0-9a-f]|1[0-9a-f]|7f)|%c2%85|%e2%80%a[89]")


def decode(value):
    return unquote(KEPT_ESCAPE.sub(lambda match: match.group().replace("%", "%25"), value))


def text(value):
    if not isinstance(value, str) or len(value.encode()) > LIMIT:
        raise ValueError("Sensitive content exceeds the redaction boundary")
    # Decode up to eight layers so encoded secrets are visible to the patterns below.
    for _ in range(8):
        decoded = decode(value)
        if decoded == value:
            break
        value = decoded
    if decode(value) != value:
        # Nobody writes a log line with more layers than that. Hide the whole value, since a secret may sit in
        # any of the layers left, and do not raise: one such line would otherwise fail every result holding it.
        return "[redacted-over-encoded]"
    for pattern, replacement in PATTERNS:
        if replacement is None:

            def assignment(match):
                original = match.group("value")
                marker = "[redacted]"
                if original.startswith(('"', "'")):
                    marker = original[0] + marker + original[0]
                return match.group(1) + marker

            value = pattern.sub(assignment, value)
        else:
            value = pattern.sub(replacement, value)

    def ipv6(match):
        try:
            ipaddress.IPv6Address(match.group())
            return "[redacted-ip]"
        except ValueError:
            return match.group()

    value = re.sub(r"(?<![A-Za-z0-9])[A-Fa-f0-9]*:[A-Fa-f0-9:]+(?![A-Za-z0-9])", ipv6, value)
    return value


def clean(value, depth=0):
    if depth > 20:
        raise ValueError("Sensitive content is too deeply nested")
    if isinstance(value, str):
        return text(value)
    if isinstance(value, list):
        return [clean(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {k: "[redacted]" if KEY.fullmatch(k) else clean(v, depth + 1) for k, v in value.items()}
    if value is None or type(value) in (bool, int, float):
        return value
    raise ValueError("Unsupported sensitive content type")


def bounded(value):
    if len(json.dumps(value, allow_nan=False).encode()) > LIMIT:
        raise ValueError("Sensitive content exceeds the redaction boundary")
    return clean(value)
