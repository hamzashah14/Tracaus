"""Byte budgets apply to the complete serialized transport payload."""

import copy
import json
import logging
import uuid

MAX_ENVELOPE_BYTES = 20_000
TRUNCATION = "\n\n[truncated; additional evidence omitted]"


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def clip_utf8(text, limit, marker=TRUNCATION):
    text = str(text).encode("utf-8", errors="replace")
    if len(text) <= limit:
        return text.decode("utf-8")
    suffix = marker.encode("utf-8")
    if len(suffix) > limit:
        return suffix[:limit].decode("utf-8", errors="ignore")
    return text[: limit - len(suffix)].decode("utf-8", errors="ignore") + marker


def error_result(code, message):
    reference = uuid.uuid4().hex[:12]
    # Never log exception messages or event/log payloads here.
    logging.getLogger("tracaus").warning("error_code=%s reference=%s", code, reference)
    return {"status": "error", "error_code": code, "request_id": reference, "message": message}


def parameters(event):
    if not isinstance(event, dict) or not isinstance(event.get("parameters", []), list):
        raise ValueError("Tool parameters must be a list of name/value objects.")
    params = {}
    for item in event.get("parameters", []):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("Each parameter needs a name and a scalar value.")
        value = item.get("value")
        if not isinstance(value, (str, int, float, type(None))) or isinstance(value, bool):
            raise ValueError("Each parameter needs a scalar value.")
        if item["name"] in params:
            raise ValueError("Duplicate parameter names are not allowed.")
        if len(item["name"]) > 64 or len(str(value)) > 8192 or len(params) >= 20:
            raise ValueError("Tool parameters exceed the input limit.")
        params[item["name"]] = value
    return params


def envelope(event, result, status=200):
    event = event if isinstance(event, dict) else {}

    def metadata(key, limit):
        value = event.get(key, "")
        return value[:limit] if isinstance(value, str) else ""

    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": metadata("actionGroup", 128),
            "apiPath": metadata("apiPath", 256),
            "httpMethod": metadata("httpMethod", 16),
            "httpStatusCode": status,
            "responseBody": {"application/json": {"body": dumps(result)}},
        },
    }


def fits(event, result, limit=MAX_ENVELOPE_BYTES):
    # Budget the conservative JSON form as well: runtimes may escape Unicode
    # and insert spaces when serializing the outer Lambda return value.
    return (
        len(json.dumps(envelope(event, result), ensure_ascii=True, allow_nan=False).encode("utf-8")) <= limit
    )


def bounded_envelope(event, result, status=200, limit=MAX_ENVELOPE_BYTES):
    from tracaus.safety import bounded

    result = copy.deepcopy(result)
    while not fits(event, result, limit):
        # Keep evidence closest to the incident. Never silently trim discovery
        # groups: their continuation belongs to the service page boundary.
        candidates = []
        for key, index in (("lines_before", 0), ("lines_after", -1), ("lines", 0)):
            if result.get(key):
                candidates.append((key, index))
        if not candidates:
            result = error_result(
                "RESPONSE_TOO_LARGE", "The result exceeds the response budget. Narrow the request."
            )
            status = 502
            break
        key, index = max(candidates, key=lambda pair: len(result[pair[0]]))
        result[key].pop(index)
        result["truncated"] = True
        result["complete"] = False
        result["status"] = "partial"
    result = bounded(result)
    if not fits(event, result, limit):
        return bounded_envelope(event, result, status, limit)
    return envelope(event, result, status)
