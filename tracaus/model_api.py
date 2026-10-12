"""Model API provider for the standalone runtime: OpenAI-compatible or Anthropic Messages.

runtime.run() drives any client exposing Bedrock's ``count_tokens`` and ``converse``; Bedrock needs no
code here. ``ModelAPI`` offers the same two methods over HTTPS and answers in Converse shapes, so
budgets, token accounting, content rules and tool handling stay in runtime.py. The API key lives in
Secrets Manager (pinned version) and never appears in config, environment, logs or exceptions.

Billable POSTs are never retried here: failures raise ``ModelAPIError`` (``kind`` only, no request or
response text) and the durable worker owns retry. Not for AgentCore, which stays Bedrock-only.

MODEL_API (JSON, all keys required except bytes_per_token):
  protocol        "openai" | "anthropic"
  base_url        https://<dns-hostname>[:port][/path]; openai appends /chat/completions,
                  anthropic appends /v1/messages (so openai usually ends in /v1, anthropic does not)
  secret_arn      full Secrets Manager ARN holding the plain API key
  secret_version  pinned VersionId of that secret
  bytes_per_token openai only: 1-8, default 3. No count endpoint exists, so input is estimated.
"""

import http.client
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from functools import lru_cache

import boto3
from botocore.config import Config

TIMEOUT = 24  # Seconds in total. runtime.run() starts inference only with >= 25 s to the deadline.
MAX_RESPONSE = 1 << 20
OVERHEAD_TOKENS = 64  # Chat-template framing that a byte-count estimate cannot see.
ANTHROPIC_VERSION = "2023-06-01"
FIELDS = {"protocol", "base_url", "secret_arn", "secret_version", "bytes_per_token"}
REQUIRED = FIELDS - {"bytes_per_token"}

# A DNS name only: no scheme tricks, userinfo, IP literal (a TLD must start with a letter), query or fragment.
_URL = re.compile(
    r"https://(?P<host>(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9])"
    r"(?::(?P<port>[0-9]{1,5}))?(?P<path>(?:/[a-z0-9._~-]+)*)/?",
    re.ASCII | re.IGNORECASE,
)
_ARN = re.compile(
    r"arn:aws:secretsmanager:[a-z]{2}(?:-[a-z]+)+-[0-9]:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]{1,512}-[A-Za-z0-9]{6}"
)
_VERSION = re.compile(r"[A-Za-z0-9-]{32,64}")
_CREDENTIAL = re.compile(r"[\x21-\x7e]{8,4096}")  # Printable ASCII: nothing that could split a header.
_OPENAI_STOP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "content_filtered",
}


class ModelAPIError(Exception):
    """A provider failure. ``kind`` is all there is: auth, bad_request, rate_limit, overloaded,
    timeout, network, bad_response or other."""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # urllib would resend Authorization / x-api-key to the redirect target.


def _opener():
    return urllib.request.build_opener(_NoRedirect)


def compact(value):
    return json.dumps(value, separators=(",", ":"))


def _loads(raw):
    def reject(constant):
        raise ValueError(constant)

    try:
        return json.loads(raw, parse_constant=reject)
    except (ValueError, RecursionError):
        raise ModelAPIError("bad_response") from None


def _check(config):
    """Strict MODEL_API validation. Messages name keys, never values."""
    if not isinstance(config, dict):
        raise ValueError("MODEL_API must be a JSON object")
    if unknown := sorted(map(str, set(config) - FIELDS)):
        raise ValueError(f"MODEL_API has unknown keys: {', '.join(unknown)}")
    if missing := sorted(REQUIRED - set(config)):
        raise ValueError(f"MODEL_API is missing keys: {', '.join(missing)}")
    if config["protocol"] not in ("openai", "anthropic"):
        raise ValueError("MODEL_API protocol must be 'openai' or 'anthropic'")
    url = config["base_url"]
    found = _URL.fullmatch(url) if isinstance(url, str) and len(url) <= 256 else None
    host = found and found["host"].lower()
    segments = found["path"].split("/") if found else []
    if (
        not found
        or host.endswith(".localhost")
        or (found["port"] and not 0 < int(found["port"]) < 65536)
        or {".", ".."} & set(segments)
    ):
        raise ValueError(
            "MODEL_API base_url must be https://<dns-hostname>[:port][/path] "
            "without credentials, IP address, query or fragment"
        )
    if not isinstance(config["secret_arn"], str) or not _ARN.fullmatch(config["secret_arn"]):
        raise ValueError("MODEL_API secret_arn must be a full Secrets Manager secret ARN")
    if not isinstance(config["secret_version"], str) or not _VERSION.fullmatch(config["secret_version"]):
        raise ValueError("MODEL_API secret_version must be a pinned secret VersionId")
    per_token = 3
    if "bytes_per_token" in config:
        per_token = config["bytes_per_token"]
        if config["protocol"] != "openai":
            raise ValueError("MODEL_API bytes_per_token applies to protocol 'openai' only")
        if type(per_token) not in (int, float) or not 1 <= per_token <= 8:
            raise ValueError("MODEL_API bytes_per_token must be a number from 1 to 8")
    port = f":{found['port']}" if found["port"] else ""
    return config["protocol"], f"https://{host}{port}{found['path']}", per_token


def _tool_data(result):
    blocks = result["content"]
    if len(blocks) == 1 and "json" in blocks[0]:
        return blocks[0]["json"]
    return "\n".join(compact(b["json"]) if "json" in b else b["text"] for b in blocks)


def _as_text(data):
    return data if isinstance(data, str) else compact(data)


def _openai_body(model, messages, system, tool_config):
    out = [{"role": "system", "content": "\n".join(s["text"] for s in system)}] if system else []
    for message in messages:
        blocks = message["content"]
        text = "\n".join(b["text"] for b in blocks if "text" in b)
        if message["role"] == "assistant":
            calls = [
                {
                    "id": use["toolUseId"],
                    "type": "function",
                    "function": {"name": use["name"], "arguments": compact(use["input"])},
                }
                for use in (b["toolUse"] for b in blocks if "toolUse" in b)
            ]
            out.append(
                {"role": "assistant", "content": text or None, **({"tool_calls": calls} if calls else {})}
            )
            continue
        for result in (b["toolResult"] for b in blocks if "toolResult" in b):
            data = _tool_data(result)
            if result.get("status") == "error":  # role:tool has no status field: carry it in the content
                data = {"tool_status": "error", "result": data}
            out.append({"role": "tool", "tool_call_id": result["toolUseId"], "content": _as_text(data)})
        if text:
            out.append({"role": "user", "content": text})
    body = {"model": model, "messages": out}
    if tools := [t["toolSpec"] for t in tool_config.get("tools", [])]:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t["inputSchema"]["json"],
                },
            }
            for t in tools
        ]
    return body


def _anthropic_block(block):
    if "text" in block:
        return {"type": "text", "text": block["text"]}
    if "toolUse" in block:
        use = block["toolUse"]
        return {"type": "tool_use", "id": use["toolUseId"], "name": use["name"], "input": use["input"]}
    result = block["toolResult"]
    return {
        "type": "tool_result",
        "tool_use_id": result["toolUseId"],
        "content": _as_text(_tool_data(result)),
        "is_error": result.get("status") == "error",
    }


def _anthropic_body(model, messages, system, tool_config):
    body = {
        "model": model,
        "messages": [
            {"role": m["role"], "content": [_anthropic_block(b) for b in m["content"]]} for m in messages
        ],
    }
    if system:
        body["system"] = [{"type": "text", "text": s["text"]} for s in system]
    if tools := [t["toolSpec"] for t in tool_config.get("tools", [])]:
        body["tools"] = [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t["inputSchema"]["json"],
            }
            for t in tools
        ]
    return body


def _tool_use(call_id, name, arguments):
    if not isinstance(call_id, str) or not isinstance(name, str):
        raise TypeError
    if isinstance(arguments, str):
        arguments = _loads(arguments) if arguments.strip() else {}
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ModelAPIError("bad_response")
    return {"toolUse": {"toolUseId": call_id, "name": name, "input": arguments}}


def _from_openai(data):
    try:
        choice = data["choices"][0]
        message, calls = choice["message"], choice["message"].get("tool_calls") or []
        text = message.get("content")
        if (text is not None and not isinstance(text, str)) or not isinstance(calls, list):
            raise TypeError
        content = [{"text": text}] if text else []
        for call in calls:
            if call.get("type", "function") != "function":
                content.append({"unsupported": str(call["type"])})
            else:
                content.append(
                    _tool_use(call["id"], call["function"]["name"], call["function"].get("arguments"))
                )
        reason = choice.get("finish_reason")
        # Some servers say "stop" with tool calls. A truncated ("length") turn is never executed.
        stop = "tool_use" if calls and reason != "length" else _OPENAI_STOP.get(reason, "unknown")
        # cached_tokens is already inside prompt_tokens: mapping it would only fake a cache charge.
        usage = data.get("usage") or {}
        counts = {
            ours: usage[theirs]
            for ours, theirs in (("inputTokens", "prompt_tokens"), ("outputTokens", "completion_tokens"))
            if type(usage.get(theirs)) is int
        }
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ModelAPIError("bad_response") from None
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop,
        "usage": counts,
    }


def _from_anthropic(data):
    try:
        content = []
        for block in data["content"]:
            if block["type"] == "text":
                content += [{"text": block["text"]}] if block["text"] else []
            elif block["type"] == "tool_use":
                content.append(_tool_use(block["id"], block["name"], block["input"]))
            else:
                content.append({"unsupported": str(block["type"])})
        usage, stop = data.get("usage") or {}, data.get("stop_reason")
        counts = {
            ours: usage[theirs]
            for ours, theirs in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"))
            if type(usage.get(theirs)) is int
        }
        # Cache fields only when present and nonzero: runtime.run() rejects those turns (fail closed).
        counts |= {
            ours: usage[theirs]
            for ours, theirs in (
                ("cacheReadInputTokens", "cache_read_input_tokens"),
                ("cacheWriteInputTokens", "cache_creation_input_tokens"),
            )
            if usage.get(theirs)
        }
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ModelAPIError("bad_response") from None
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop if isinstance(stop, str) else "unknown",
        "usage": counts,
    }


def _status_kind(status):
    if status in (401, 403):
        return "auth"
    if status in (400, 413, 422):
        return "bad_request"
    if status == 429:
        return "rate_limit"
    return "overloaded" if status >= 500 else "other"


class ModelAPI:
    def __init__(self, config, *, secrets_client=None, opener=None):
        self.protocol, self.base_url, self._bytes_per_token = _check(config)
        self._secret = (config["secret_arn"], config["secret_version"])
        self._secrets, self._opener, self._key = secrets_client, opener or _opener(), None

    def __repr__(self):
        return f"ModelAPI({self.protocol}, {self.base_url})"

    def _credential(self):
        """Read once per instance; the pinned version is immutable so there is nothing to expire."""
        if self._key is None:
            arn, version = self._secret
            try:
                client = self._secrets or boto3.client(
                    "secretsmanager",
                    region_name=arn.split(":")[3],
                    config=Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 1}),
                )
                response = client.get_secret_value(SecretId=arn, VersionId=version)
                value = response["SecretString"].strip()
                if response.get("ARN") != arn or response.get("VersionId") != version:
                    raise ValueError
                if not _CREDENTIAL.fullmatch(value):
                    raise ValueError
            except Exception:
                raise ModelAPIError("auth") from None
            self._key = value
        return self._key

    def _post(self, path, body):
        key = self._credential()
        headers = (
            {"Authorization": f"Bearer {key}"}
            if self.protocol == "openai"
            else {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
        )
        request = urllib.request.Request(
            self.base_url + path,
            compact(body).encode(),
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "tracaus-model-api/1",
                **headers,
            },
            method="POST",
        )
        raw, start = bytearray(), time.monotonic()
        try:
            # The socket timeout bounds each wait; the loop bounds the whole response (slow drip).
            with self._opener.open(request, timeout=TIMEOUT) as response:
                while chunk := response.read(65536):
                    raw += chunk
                    if len(raw) > MAX_RESPONSE:
                        raise ModelAPIError("bad_response")
                    if time.monotonic() - start > TIMEOUT:
                        raise ModelAPIError("timeout")
        except ModelAPIError:
            raise
        except urllib.error.HTTPError as exc:
            exc.close()
            raise ModelAPIError(_status_kind(exc.code)) from None
        except OSError as exc:
            timed_out = isinstance(exc, TimeoutError) or isinstance(
                getattr(exc, "reason", None), TimeoutError
            )
            raise ModelAPIError("timeout" if timed_out else "network") from None
        except http.client.HTTPException:
            raise ModelAPIError("network") from None
        except Exception:
            raise ModelAPIError("other") from None
        data = _loads(bytes(raw))
        if not isinstance(data, dict):
            raise ModelAPIError("bad_response")
        return data

    def count_tokens(self, *, modelId, input):
        request = input["converse"]
        parts = (modelId, request["messages"], request.get("system", []), request.get("toolConfig", {}))
        if self.protocol == "openai":
            # No count endpoint: bytes / bytes_per_token plus template overhead. An estimate, not a bound;
            # runtime.run() stops the run if the provider later reports more input than this.
            size = len(compact(_openai_body(*parts)).encode())
            return {"inputTokens": math.ceil(size / self._bytes_per_token) + OVERHEAD_TOKENS}
        count = self._post("/v1/messages/count_tokens", _anthropic_body(*parts)).get("input_tokens")
        if type(count) is not int or count < 1:
            raise ModelAPIError("bad_response")
        return {"inputTokens": count}

    def converse(self, *, modelId, messages, system, toolConfig, inferenceConfig):
        limit = inferenceConfig["maxTokens"]
        if self.protocol == "openai":
            # max_completion_tokens bounds reasoning tokens too; some older compatible servers only
            # know max_tokens and would ignore this, which runtime.run() then catches on output usage.
            body = {**_openai_body(modelId, messages, system, toolConfig), "max_completion_tokens": limit}
            return _from_openai(self._post("/chat/completions", body))
        body = {**_anthropic_body(modelId, messages, system, toolConfig), "max_tokens": limit}
        return _from_anthropic(self._post("/v1/messages", body))


@lru_cache(maxsize=2)
def _build(raw):
    try:
        config = json.loads(raw)
    except ValueError:
        raise ValueError("MODEL_API is not valid JSON") from None
    return ModelAPI(config)


parse = _build  # validated, cached ModelAPI for a raw MODEL_API string; raises ValueError


def from_env():
    """None selects Bedrock (MODEL_API unset or empty). One instance per distinct setting, so a warm
    container reads the secret once."""
    raw = os.environ.get("MODEL_API", "").strip()
    return _build(raw) if raw else None
