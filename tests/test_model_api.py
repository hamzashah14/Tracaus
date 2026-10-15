"""Model API provider (OpenAI-compatible / Anthropic): offline; fake HTTP layer and fake Secrets Manager."""

import http.client
import io
import json
import logging
import math
import time
import urllib.error
import urllib.request
import urllib.response
from email.message import Message
from unittest.mock import MagicMock
from urllib.parse import urlsplit

import pytest
from botocore.exceptions import ClientError

from tracaus import agentcore_host, diagnosis, model_api, runtime

REGION = "eu-central-1"
ARN = f"arn:aws:secretsmanager:{REGION}:123456789012:secret:tracaus-model-AbCdEf"
VERSION = "11111111-2222-3333-4444-555555555555"
CREDENTIAL = "fake-model-credential-for-offline-tests"
IID = "i-0123456789abcdef0"
MISMATCH = "TOKEN_ACCOUNTING_MISMATCH"


def compact(value):
    return json.dumps(value, separators=(",", ":"))


class FakeResponse:
    def __init__(self, body, status=200):
        self.status, self._body = status, io.BytesIO(body)

    def read1(self, amount=-1):
        return self._body.read1(amount)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def ok(payload):
    return FakeResponse(json.dumps(payload).encode())


class FakeOpener:
    """Replays queued replies (response objects or exceptions) and records every request."""

    def __init__(self, replies=()):
        self.replies, self.requests, self.events = list(replies), [], []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.events.append("http:" + urlsplit(request.full_url).path)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class FakeSecrets:
    def __init__(self, value=CREDENTIAL, arn=ARN, version=VERSION):
        self.value, self.arn, self.version, self.calls = value, arn, version, []

    def get_secret_value(self, **kwargs):
        self.calls.append(kwargs)
        return {"ARN": self.arn, "VersionId": self.version, "SecretString": self.value}


def config(protocol="openai", **extra):
    base = "https://api.example.com/v1" if protocol == "openai" else "https://api.example.com"
    return {"protocol": protocol, "base_url": base, "secret_arn": ARN, "secret_version": VERSION, **extra}


def build(protocol, *replies, secrets=None, **extra):
    opener = FakeOpener(replies)
    return model_api.ModelAPI(
        config(protocol, **extra), secrets_client=secrets or FakeSecrets(), opener=opener
    ), opener


TOOLS = {
    "tools": [
        {
            "toolSpec": {
                "name": "fetch_logs",
                "description": "Fetch logs",
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {"instance_id": {"type": "string"}},
                        "required": ["instance_id"],
                    }
                },
            }
        }
    ]
}
SCHEMA = TOOLS["tools"][0]["toolSpec"]["inputSchema"]["json"]
SYSTEM = [{"text": "Be careful."}]
CALL = {"toolUseId": "call_1", "name": "fetch_logs", "input": {"instance_id": IID}}
MESSAGES = [
    {"role": "user", "content": [{"text": "Investigate"}]},
    {
        "role": "assistant",
        "content": [
            {"text": "Checking logs."},
            {"toolUse": CALL},
            {"toolUse": {**CALL, "toolUseId": "call_2"}},
        ],
    },
    {
        "role": "user",
        "content": [
            {
                "toolResult": {
                    "toolUseId": "call_1",
                    "content": [{"json": {"status": "ok"}}],
                    "status": "success",
                }
            },
            {
                "toolResult": {
                    "toolUseId": "call_2",
                    "content": [{"json": {"status": "bad"}}],
                    "status": "error",
                }
            },
        ],
    },
]


def converse(api, tokens=512):
    return api.converse(
        modelId="model-v1",
        messages=MESSAGES,
        system=SYSTEM,
        toolConfig=TOOLS,
        inferenceConfig={"maxTokens": tokens},
    )


def openai_reply(content="done", calls=None, finish="stop", usage=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    usage = {"prompt_tokens": 100, "completion_tokens": 20} if usage is None else usage
    return {"choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage}


def openai_call(call_id="call_1", args=None, name="fetch_logs"):
    args = compact({"instance_id": IID}) if args is None else args
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


def anthropic_reply(content=None, stop="end_turn", usage=None):
    usage = {"input_tokens": 100, "output_tokens": 20} if usage is None else usage
    return {"content": content or [{"type": "text", "text": "done"}], "stop_reason": stop, "usage": usage}


def sent(opener, index=0):
    request = opener.requests[index]
    return request, json.loads(request.data)


def toolu(call_id="toolu_1"):
    return {"type": "tool_use", "id": call_id, "name": "fetch_logs", "input": {"instance_id": IID}}


# 1. OpenAI-compatible translation ------------------------------------------------------------


def test_openai_request_translation():
    api, opener = build("openai", ok(openai_reply()))
    converse(api)
    request, body = sent(opener)
    assert request.full_url == "https://api.example.com/v1/chat/completions"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {CREDENTIAL}"
    assert request.get_header("X-api-key") is None
    assert body["model"] == "model-v1" and body["max_completion_tokens"] == 512 and "max_tokens" not in body
    assert body["tools"] == [
        {
            "type": "function",
            "function": {"name": "fetch_logs", "description": "Fetch logs", "parameters": SCHEMA},
        }
    ]
    arguments = compact({"instance_id": IID})
    assert body["messages"] == [
        {"role": "system", "content": "Be careful."},
        {"role": "user", "content": "Investigate"},
        {
            "role": "assistant",
            "content": "Checking logs.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "fetch_logs", "arguments": arguments},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "fetch_logs", "arguments": arguments},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": compact({"status": "ok"})},
        {
            "role": "tool",
            "tool_call_id": "call_2",
            "content": compact({"tool_status": "error", "result": {"status": "bad"}}),
        },
    ]


def test_openai_tool_only_assistant_turn_has_null_content():
    api, opener = build("openai", ok(openai_reply()))
    api.converse(
        modelId="m",
        messages=[{"role": "assistant", "content": [{"toolUse": CALL}]}],
        system=[],
        toolConfig={},
        inferenceConfig={"maxTokens": 5},
    )
    body = sent(opener)[1]
    assert body["messages"][0]["content"] is None and "tools" not in body
    assert all(m["role"] != "system" for m in body["messages"])


def test_openai_response_tool_call_and_usage_ignore_cached_tokens():
    usage = {"prompt_tokens": 90, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 40}}
    api, _ = build("openai", ok(openai_reply(None, [openai_call()], "tool_calls", usage)))
    assert converse(api) == {
        "output": {"message": {"role": "assistant", "content": [{"toolUse": CALL}]}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": 90, "outputTokens": 10},
    }


@pytest.mark.parametrize(
    "calls,finish,stop",
    [
        (None, "stop", "end_turn"),
        (None, "length", "max_tokens"),
        (None, "tool_calls", "tool_use"),
        (None, "content_filter", "content_filtered"),
        (None, "something_new", "unknown"),
        ([openai_call()], "stop", "tool_use"),  # some servers say "stop" with tool calls
        ([openai_call()], "tool_calls", "tool_use"),
        ([openai_call()], "length", "max_tokens"),  # truncated: never forced into execution
    ],
)
def test_openai_stop_reason_mapping(calls, finish, stop):
    api, _ = build("openai", ok(openai_reply("t", calls, finish)))
    assert converse(api)["stopReason"] == stop


def test_openai_text_and_tool_blocks_keep_order_and_missing_usage_fails_closed():
    api, _ = build("openai", ok({**openai_reply("Looking.", [openai_call()]), "usage": None}))
    result = converse(api)
    assert result["output"]["message"]["content"] == [{"text": "Looking."}, {"toolUse": CALL}]
    assert result["usage"] == {}  # runtime.run() stops on TOKEN_ACCOUNTING_MISMATCH


@pytest.mark.parametrize(
    "arguments,expected",
    [
        ("", {}),
        (None, {}),
        ("{}", {}),
        (compact({"instance_id": IID}), {"instance_id": IID}),
        ({"a": 1}, {"a": 1}),
    ],
)
def test_openai_tool_arguments_parse(arguments, expected):
    call = openai_call(args="")
    call["function"]["arguments"] = arguments
    api, _ = build("openai", ok(openai_reply(None, [call], "tool_calls")))
    content = converse(api)["output"]["message"]["content"]
    assert content == [{"toolUse": {"toolUseId": "call_1", "name": "fetch_logs", "input": expected}}]


@pytest.mark.parametrize("arguments", ["{not json", "[]", "3", '"x"', '{"a": NaN}', 7])
def test_openai_invalid_tool_arguments_raise(arguments):
    call = openai_call()
    call["function"]["arguments"] = arguments
    api, _ = build("openai", ok(openai_reply(None, [call], "tool_calls")))
    with pytest.raises(model_api.ModelAPIError) as caught:
        converse(api)
    assert caught.value.kind == "bad_response"


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [{}]}, {"choices": ["x"]}, []])
def test_openai_unexpected_shape_raises(payload):
    api, _ = build("openai", ok(payload))
    with pytest.raises(model_api.ModelAPIError) as caught:
        converse(api)
    assert caught.value.kind == "bad_response"


def test_openai_unsupported_tool_type_is_left_for_the_runtime_to_reject():
    call = {**openai_call(), "type": "custom"}
    api, _ = build("openai", ok(openai_reply(None, [call], "tool_calls")))
    assert converse(api)["output"]["message"]["content"] == [{"unsupported": "custom"}]


def test_openai_count_is_a_local_conservative_estimate_without_http_or_secret():
    secrets = FakeSecrets()
    request = {"messages": MESSAGES, "system": SYSTEM, "toolConfig": TOOLS}
    api, opener = build("openai", ok(openai_reply()), secrets=secrets)
    count = api.count_tokens(modelId="model-v1", input={"converse": request})["inputTokens"]
    assert type(count) is int and count >= 1 and not opener.requests and not secrets.calls
    converse(api)
    body = sent(opener)[1]
    del body["max_completion_tokens"]
    assert count == math.ceil(len(compact(body).encode()) / 3) + 64
    tight = build("openai", bytes_per_token=1.5)[0]
    loose = build("openai", bytes_per_token=6)[0]
    counted = [
        a.count_tokens(modelId="model-v1", input={"converse": request})["inputTokens"] for a in (tight, loose)
    ]
    assert counted[0] > count > counted[1] >= 65
    tiny = api.count_tokens(modelId="m", input={"converse": {"messages": [], "system": [], "toolConfig": {}}})
    assert tiny["inputTokens"] >= 64


# 2. Anthropic Messages translation -----------------------------------------------------------


def test_anthropic_request_translation():
    api, opener = build("anthropic", ok(anthropic_reply()))
    converse(api)
    request, body = sent(opener)
    assert request.full_url == "https://api.example.com/v1/messages"
    assert request.get_header("X-api-key") == CREDENTIAL
    assert request.get_header("Anthropic-version") == "2023-06-01"
    assert request.get_header("Authorization") is None
    assert body["model"] == "model-v1" and body["max_tokens"] == 512
    assert body["system"] == [{"type": "text", "text": "Be careful."}]
    assert body["tools"] == [{"name": "fetch_logs", "description": "Fetch logs", "input_schema": SCHEMA}]
    use = {"type": "tool_use", "name": "fetch_logs", "input": {"instance_id": IID}}
    assert body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Investigate"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking logs."},
                {**use, "id": "call_1"},
                {**use, "id": "call_2"},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": compact({"status": "ok"}),
                    "is_error": False,
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "call_2",
                    "content": compact({"status": "bad"}),
                    "is_error": True,
                },
            ],
        },
    ]


def test_anthropic_response_translation_and_usage():
    content = [{"type": "text", "text": "Looking."}, toolu("call_1")]
    usage = {
        "input_tokens": 90,
        "output_tokens": 10,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    api, _ = build("anthropic", ok(anthropic_reply(content, "tool_use", usage)))
    assert converse(api) == {
        "output": {"message": {"role": "assistant", "content": [{"text": "Looking."}, {"toolUse": CALL}]}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": 90, "outputTokens": 10},
    }


def test_anthropic_cache_usage_is_mapped_only_when_nonzero_so_the_runtime_rejects_it():
    usage = {
        "input_tokens": 9,
        "output_tokens": 1,
        "cache_creation_input_tokens": 7,
        "cache_read_input_tokens": 5,
    }
    api, _ = build("anthropic", ok(anthropic_reply(usage=usage)))
    assert converse(api)["usage"] == {
        "inputTokens": 9,
        "outputTokens": 1,
        "cacheReadInputTokens": 5,
        "cacheWriteInputTokens": 7,
    }


@pytest.mark.parametrize("stop", ["end_turn", "tool_use", "max_tokens", "refusal"])
def test_anthropic_stop_reasons_pass_through(stop):
    api, _ = build("anthropic", ok(anthropic_reply(stop=stop)))
    assert converse(api)["stopReason"] == stop


def test_anthropic_unknown_block_and_bad_shapes():
    api, _ = build("anthropic", ok(anthropic_reply([{"type": "thinking", "thinking": "x"}])))
    assert converse(api)["output"]["message"]["content"] == [{"unsupported": "thinking"}]
    for payload in (
        {},
        {"content": "x"},
        {"content": [{"type": "tool_use", "id": "a", "name": "n", "input": []}]},
    ):
        api, _ = build("anthropic", ok(payload))
        with pytest.raises(model_api.ModelAPIError) as caught:
            converse(api)
        assert caught.value.kind == "bad_response"


def test_anthropic_count_uses_the_provider_endpoint():
    api, opener = build("anthropic", ok({"input_tokens": 321}))
    request = {"messages": MESSAGES, "system": SYSTEM, "toolConfig": TOOLS}
    assert api.count_tokens(modelId="model-v1", input={"converse": request}) == {"inputTokens": 321}
    http_request, body = sent(opener)
    assert http_request.full_url == "https://api.example.com/v1/messages/count_tokens"
    assert http_request.get_header("X-api-key") == CREDENTIAL
    assert "max_tokens" not in body and body["model"] == "model-v1" and body["tools"] and body["system"]


@pytest.mark.parametrize("reply", [{}, {"input_tokens": 0}, {"input_tokens": True}, {"input_tokens": "9"}])
def test_anthropic_count_must_be_a_positive_integer(reply):
    api, _ = build("anthropic", ok(reply))
    with pytest.raises(model_api.ModelAPIError) as caught:
        api.count_tokens(
            modelId="m", input={"converse": {"messages": MESSAGES, "system": [], "toolConfig": {}}}
        )
    assert caught.value.kind == "bad_response"


# 3. runtime.run() end to end over the fake HTTP layer ----------------------------------------

REPORT = {
    "version": 1,
    "findings": [{"kind": "insufficient_data", "statement": "Coverage insufficient", "evidence_ids": []}],
    "facts": [],
    "hypotheses": [],
    "limitations": ["No independent corroboration"],
    "recommendations": ["Operator review"],
}
TOOL_RESULT = {
    "status": "ok",
    "complete": True,
    "instance_id": IID,
    "log_group": "/synthetic/application",
    "window_start": "2026-10-01T10:00:00Z",
    "window_end": "2026-10-01T10:15:00Z",
    "lines": ["synthetic line"],
}


def drive(api, opener, tools=None, **kwargs):
    policy = runtime.Limits()
    budget = runtime.MemoryBudget(policy)

    def reserve(delta):
        opener.events.append("reserve")
        return budget.reserve(delta)

    return runtime.run(
        "Investigate",
        model_id="model-v1",
        region=REGION,
        tools=tools or MagicMock(),
        reserve=reserve,
        limits=policy,
        deadline=time.time() + 180,
        client=api,
        **kwargs,
    )


def test_run_counts_then_reserves_then_infers_openai():
    api, opener = build("openai", ok(openai_reply("Evidence is inconclusive.")))
    result = drive(api, opener)
    assert result["complete"] and result["usage"] == {"input_tokens": 100, "output_tokens": 20}
    assert opener.events == ["reserve", "http:/v1/chat/completions"]  # estimate is local


def test_run_counts_then_reserves_then_infers_anthropic():
    api, opener = build(
        "anthropic",
        ok({"input_tokens": 100}),
        ok(anthropic_reply([{"type": "text", "text": "Evidence is inconclusive."}])),
    )
    assert drive(api, opener)["complete"]
    assert opener.events == ["http:/v1/messages/count_tokens", "reserve", "http:/v1/messages"]


def test_run_openai_cached_tokens_inside_prompt_tokens_do_not_trip_accounting():
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 60}}
    api, opener = build("openai", ok(openai_reply("Evidence is inconclusive.", usage=usage)))
    assert drive(api, opener)["complete"]


@pytest.mark.parametrize(
    "protocol,replies",
    [
        (
            "openai",
            [
                ok(
                    openai_reply(
                        None, [openai_call()], "tool_calls", {"prompt_tokens": 10**6, "completion_tokens": 1}
                    )
                )
            ],
        ),
        (
            "openai",
            [
                ok(
                    openai_reply(
                        None, [openai_call()], "tool_calls", {"prompt_tokens": 10, "completion_tokens": 2000}
                    )
                )
            ],
        ),
        ("openai", [ok(openai_reply(None, [openai_call()], "tool_calls", {}))]),
        (
            "anthropic",
            [
                ok({"input_tokens": 100}),
                ok(anthropic_reply([toolu()], "tool_use", {"input_tokens": 101, "output_tokens": 1})),
            ],
        ),
        (
            "anthropic",
            [
                ok({"input_tokens": 100}),
                ok(
                    anthropic_reply(
                        [toolu()],
                        "tool_use",
                        {"input_tokens": 9, "output_tokens": 1, "cache_read_input_tokens": 1},
                    )
                ),
            ],
        ),
    ],
)
def test_run_usage_mismatch_stops_before_any_tool(protocol, replies):
    api, opener = build(protocol, *replies)
    tools = MagicMock()
    result = drive(api, opener, tools)
    assert result["code"] == MISMATCH and not result["complete"]
    tools.invoke.assert_not_called()
    assert len(opener.requests) == len(replies)  # one billable call, no retry


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_run_tool_use_then_structured_diagnosis(protocol, monkeypatch):
    monkeypatch.setenv("TRACAUS_DIAGNOSTIC_POLICY", diagnosis.VERSION)
    tools = MagicMock()
    tools.invoke.return_value = (TOOL_RESULT, True)
    if protocol == "openai":
        replies = [
            ok(
                openai_reply(
                    None, [openai_call("call_1")], "stop", {"prompt_tokens": 100, "completion_tokens": 20}
                )
            ),
            ok(
                openai_reply(
                    json.dumps(REPORT), None, "stop", {"prompt_tokens": 150, "completion_tokens": 30}
                )
            ),
        ]
    else:
        replies = [
            ok({"input_tokens": 100}),
            ok(anthropic_reply([toolu("call_1")], "tool_use", {"input_tokens": 100, "output_tokens": 20})),
            ok({"input_tokens": 150}),
            ok(
                anthropic_reply(
                    [{"type": "text", "text": json.dumps(REPORT)}],
                    "end_turn",
                    {"input_tokens": 150, "output_tokens": 30},
                )
            ),
        ]
    api, opener = build(protocol, *replies)
    result = drive(api, opener, tools)
    assert result["complete"] and result["diagnosis"]["status"] == "VALID"
    assert "Sources and observation windows" in result["text"]
    assert result["usage"] == {"input_tokens": 250, "output_tokens": 50}
    assert result["tools"] == ["fetch_logs"]
    assert tools.invoke.call_count == 1 and tools.invoke.call_args.args[:2] == (
        "fetch_logs",
        {"instance_id": IID},
    )
    last = json.loads(opener.requests[-1].data)
    assert "call_1" in json.dumps(last["messages"]) and "synthetic line" in json.dumps(last["messages"])


def test_run_prefers_model_api_when_configured_and_keeps_bedrock_otherwise(monkeypatch):
    api, opener = build("openai", ok(openai_reply("Evidence is inconclusive.")))
    monkeypatch.setattr(model_api, "from_env", lambda: api)
    monkeypatch.setattr(
        runtime, "sdk_client", MagicMock(side_effect=AssertionError("Bedrock must not be used"))
    )
    assert drive(None, opener)["complete"] and len(opener.requests) == 1

    bedrock = MagicMock()
    bedrock.count_tokens.return_value = {"inputTokens": 100}
    bedrock.converse.return_value = {
        "usage": {"inputTokens": 100, "outputTokens": 20},
        "stopReason": "end_turn",
        "output": {"message": {"role": "assistant", "content": [{"text": "Evidence is inconclusive."}]}},
    }
    factory = MagicMock(return_value=bedrock)
    monkeypatch.setattr(model_api, "from_env", lambda: None)
    monkeypatch.setattr(runtime, "sdk_client", factory)
    assert drive(None, opener)["complete"]
    factory.assert_called_once_with("bedrock-runtime", REGION)


# 4. HTTP behaviour ---------------------------------------------------------------------------


def http_error(status):
    body = io.BytesIO(b"SENTINEL-BODY " + CREDENTIAL.encode())
    return urllib.error.HTTPError("https://api.example.com/x", status, "SENTINEL-MESSAGE", Message(), body)


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.parametrize(
    "status,kind",
    [
        (400, "bad_request"),
        (401, "auth"),
        (403, "auth"),
        (413, "bad_request"),
        (422, "bad_request"),
        (429, "rate_limit"),
        (500, "overloaded"),
        (503, "overloaded"),
        (529, "overloaded"),
        (404, "other"),
        (302, "other"),
    ],
)
def test_http_status_maps_to_kind_with_exactly_one_call(protocol, status, kind, caplog):
    caplog.set_level(logging.DEBUG)
    api, opener = build(protocol, http_error(status))
    with pytest.raises(model_api.ModelAPIError) as caught:
        converse(api)
    error = caught.value
    assert error.kind == kind and len(opener.requests) == 1  # billable POST: never retried
    assert error.__cause__ is None and error.__suppress_context__
    shown = f"{error!s} {error!r} {caplog.text}"
    assert "SENTINEL" not in shown and CREDENTIAL not in shown


@pytest.mark.parametrize(
    "failure,kind",
    [
        (TimeoutError("SENTINEL"), "timeout"),
        (urllib.error.URLError(TimeoutError("SENTINEL")), "timeout"),
        (urllib.error.URLError("SENTINEL dns"), "network"),
        (ConnectionResetError("SENTINEL"), "network"),
        (http.client.IncompleteRead(b"SENTINEL"), "network"),
        (RuntimeError("SENTINEL " + CREDENTIAL), "other"),
    ],
)
def test_transport_failures_map_to_kind_without_detail(failure, kind):
    api, opener = build("openai", failure)
    with pytest.raises(model_api.ModelAPIError) as caught:
        converse(api)
    assert caught.value.kind == kind and len(opener.requests) == 1
    assert (
        "SENTINEL" not in f"{caught.value!s} {caught.value!r}"
        and caught.value.__cause__ is None
        and caught.value.__suppress_context__
    )


def test_response_limits_and_json_only():
    for body in (b"x" * (model_api.MAX_RESPONSE + 1), b"<html>no</html>", b"[]", b'{"a": NaN}', b""):
        api, _ = build("openai", FakeResponse(body))
        with pytest.raises(model_api.ModelAPIError) as caught:
            converse(api)
        assert caught.value.kind == "bad_response"
    padded = json.dumps(openai_reply()).encode().ljust(model_api.MAX_RESPONSE)
    assert converse(build("openai", FakeResponse(padded))[0])["stopReason"] == "end_turn"


def test_total_time_is_bounded_not_just_each_socket_read(monkeypatch):
    assert model_api.TIMEOUT < 25  # runtime.run() starts inference only with at least 25 s left
    monkeypatch.setattr(model_api, "TIMEOUT", -1)
    api, _ = build("openai", ok(openai_reply()))
    with pytest.raises(model_api.ModelAPIError) as caught:
        converse(api)
    assert caught.value.kind == "timeout"


def test_a_slow_drip_is_cut_off_while_bytes_are_still_arriving(monkeypatch):
    """A real HTTPResponse over a socket pair: one byte every 0.25 s for four seconds, TIMEOUT of one second."""
    import http.client
    import socket
    import threading
    import time

    server, client = socket.socketpair()

    def drip():
        try:
            server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
            for _ in range(16):
                server.sendall(b"x")
                time.sleep(0.25)
        except OSError:
            pass  # the client gave up and closed its end
        finally:
            server.close()

    threading.Thread(target=drip, daemon=True).start()
    response = http.client.HTTPResponse(client)
    response.begin()

    class Opener:
        def open(self, request, timeout=None):
            return response

    monkeypatch.setattr(model_api, "TIMEOUT", 1)
    api = model_api.ModelAPI(config(), secrets_client=FakeSecrets(), opener=Opener())
    started = time.monotonic()
    with pytest.raises(model_api.ModelAPIError) as caught:
        converse(api)
    assert caught.value.kind == "timeout"
    assert time.monotonic() - started < 2.5  # read(65536) held it for the whole four seconds


def test_requests_are_json_posts_with_the_socket_timeout():
    seen = {}

    class Recording(FakeOpener):
        def open(self, request, timeout=None):
            seen["timeout"] = timeout
            return super().open(request, timeout)

    opener = Recording([ok(openai_reply())])
    api = model_api.ModelAPI(config(), secrets_client=FakeSecrets(), opener=opener)
    converse(api)
    assert seen["timeout"] == model_api.TIMEOUT
    request = opener.requests[0]
    assert request.get_header("Content-type") == "application/json"
    assert request.data == compact(json.loads(request.data)).encode()  # compact UTF-8 JSON


def test_redirects_are_refused_and_never_followed():
    hits = []

    class Redirecting(urllib.request.HTTPSHandler):
        handler_order = 100  # ahead of the real handler, which could open a socket

        def https_open(self, request):
            hits.append(request.full_url)
            headers = Message()
            headers["Location"] = "https://elsewhere.example.net/steal"
            response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
            response.msg = "Found"
            return response

    opener = model_api._opener()
    opener.add_handler(Redirecting())
    api = model_api.ModelAPI(config(), secrets_client=FakeSecrets(), opener=opener)
    with pytest.raises(model_api.ModelAPIError) as caught:
        converse(api)
    assert caught.value.kind == "other" and hits == ["https://api.example.com/v1/chat/completions"]


def test_default_opener_refuses_redirect_requests():
    handler = next(
        h for h in model_api._opener().handlers if isinstance(h, urllib.request.HTTPRedirectHandler)
    )
    request = urllib.request.Request("https://api.example.com/v1/x", b"{}")
    assert handler.redirect_request(request, None, 302, "Found", Message(), "https://e.example.net/") is None


GOOD_URLS = [
    ("https://api.openai.com/v1", "https://api.openai.com/v1"),
    ("https://API.Example.com", "https://api.example.com"),
    ("https://llm.internal.example.com:8443/v1/", "https://llm.internal.example.com:8443/v1"),
    ("https://gateway.example.co.uk/openai/v1", "https://gateway.example.co.uk/openai/v1"),
]
BAD_URLS = [
    "",
    None,
    "http://api.example.com/v1",
    "ftp://api.example.com/v1",
    "//api.example.com/v1",
    "api.example.com/v1",
    "https://localhost/v1",
    "https://model.localhost/v1",
    "https://intranet/v1",
    "https://127.0.0.1/v1",
    "https://10.0.0.5:8443/v1",
    "https://[::1]/v1",
    "https://0x7f.1/v1",
    "https://2130706433/v1",
    "https://trusted.example.com@evil.example.net/v1",
    "https://api.example.com/v1?x=1",
    "https://api.example.com/v1#frag",
    "https://api.example.com/v1/../admin",
    "https://api.example.com/./v1",
    "https://api.example.com//v1",
    "https://api.example.com/v 1",
    "https://api.example.com/v1\n",
    "https://api.example.com/v%31",
    "https://api.example.com:99999/v1",
    "https://api.example.com:/v1",
    "https://-bad.example.com/v1",
    "https://api.example.com./v1",
    "https://api.exämple.com/v1",
    "https://api.example.com/" + "a" * 300,
]


@pytest.mark.parametrize("given,normalized", GOOD_URLS)
def test_base_url_accepts_https_dns_names_and_normalizes(given, normalized):
    assert (
        model_api.ModelAPI({**config(), "base_url": given}, secrets_client=FakeSecrets()).base_url
        == normalized
    )


@pytest.mark.parametrize("url", BAD_URLS)
def test_base_url_rejects_everything_else(url):
    with pytest.raises(ValueError, match="base_url"):
        model_api.ModelAPI({**config(), "base_url": url})


# 5. Secret handling --------------------------------------------------------------------------


def test_secret_is_read_once_pinned_and_never_exposed(caplog):
    caplog.set_level(logging.DEBUG)
    secrets = FakeSecrets("  " + CREDENTIAL + "\n")
    api, opener = build("openai", ok(openai_reply()), ok(openai_reply()), secrets=secrets)
    converse(api)
    converse(api)
    assert secrets.calls == [{"SecretId": ARN, "VersionId": VERSION}]
    assert [r.get_header("Authorization") for r in opener.requests] == [f"Bearer {CREDENTIAL}"] * 2
    shown = f"{api!r} {api!s} {caplog.text}"
    assert CREDENTIAL not in shown and ARN not in repr(api) and "openai" in repr(api)


@pytest.mark.parametrize(
    "secrets",
    [
        FakeSecrets(arn=ARN.replace("AbCdEf", "ZzZzZz")),
        FakeSecrets(version="99999999-2222-3333-4444-555555555555"),
        FakeSecrets(value=""),
        FakeSecrets(value="   \n"),
        FakeSecrets(value="short"),
        FakeSecrets(value="contains whitespace SENTINEL-" + CREDENTIAL),
        FakeSecrets(value=CREDENTIAL + "\r\nX-Injected: 1"),
        FakeSecrets(value=None),
        FakeSecrets(value=b"binary"),
        FakeSecrets(value="x" * 5000),
    ],
)
def test_secret_with_wrong_identity_or_shape_is_refused_before_any_request(secrets):
    api, opener = build("openai", ok(openai_reply()), secrets=secrets)
    with pytest.raises(model_api.ModelAPIError) as caught:
        converse(api)
    assert caught.value.kind == "auth" and not opener.requests
    assert (
        "SENTINEL" not in f"{caught.value!s} {caught.value!r}"
        and caught.value.__cause__ is None
        and caught.value.__suppress_context__
    )


def test_secret_service_errors_are_auth_failures_without_detail():
    class Denied:
        def get_secret_value(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "SENTINEL " + ARN}}, "Get"
            )

    api, opener = build("anthropic", ok(anthropic_reply()), secrets=Denied())
    with pytest.raises(model_api.ModelAPIError) as caught:
        converse(api)
    assert caught.value.kind == "auth" and "SENTINEL" not in repr(caught.value) and not opener.requests


def test_secrets_client_is_created_in_the_secrets_region(monkeypatch):
    made = []

    def factory(service, **kwargs):
        made.append((service, kwargs["region_name"]))
        return FakeSecrets()

    monkeypatch.setattr(model_api.boto3, "client", factory)
    api = model_api.ModelAPI(config(), opener=FakeOpener([ok(openai_reply()), ok(openai_reply())]))
    converse(api)
    converse(api)
    assert made == [("secretsmanager", REGION)]


# 6. MODEL_API environment parsing ------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "  "])
def test_unset_or_empty_selects_bedrock(monkeypatch, value):
    monkeypatch.delenv("MODEL_API", raising=False)
    if value is not None:
        monkeypatch.setenv("MODEL_API", value)
    assert model_api.from_env() is None


def test_from_env_builds_the_configured_provider(monkeypatch):
    monkeypatch.setenv("MODEL_API", json.dumps(config("openai", bytes_per_token=2.5)))
    api = model_api.from_env()
    assert (api.protocol, api.base_url) == ("openai", "https://api.example.com/v1")
    assert model_api.from_env() is api  # a warm container keeps one provider and one secret read
    monkeypatch.setenv("MODEL_API", json.dumps(config("anthropic")))
    assert model_api.from_env().protocol == "anthropic"


NO_SUFFIX = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:tracaus-model"
SSM_ARN = "arn:aws:ssm:eu-central-1:123456789012:parameter/model"
FLOATING = "AWSCURRENT"


def raw(**changes):
    return json.dumps({**config(), **changes})


@pytest.mark.parametrize(
    "value,message",
    [
        ("{not json", "valid JSON"),
        ("[]", "JSON object"),
        ('"x"', "JSON object"),
        (raw(credential="SENTINEL-VALUE"), "unknown keys: credential"),
        (raw(credential="SENTINEL-VALUE", bearer="x"), "bearer, credential"),
        (json.dumps({k: v for k, v in config().items() if k != "secret_arn"}), "missing keys: secret_arn"),
        (raw(protocol="bedrock"), "protocol"),
        (raw(protocol="OpenAI"), "protocol"),
        (raw(protocol=None), "protocol"),
        (raw(secret_arn=NO_SUFFIX), "secret_arn"),
        (raw(secret_arn=SSM_ARN), "secret_arn"),
        (raw(secret_arn=5), "secret_arn"),
        (raw(secret_version=FLOATING), "secret_version"),
        (raw(secret_version=""), "secret_version"),
        (json.dumps(config("anthropic", bytes_per_token=3)), "bytes_per_token"),
        *[
            (json.dumps(config(bytes_per_token=bad)), "bytes_per_token")
            for bad in (True, "3", None, 0, 0.5, 8.5, -3, [3])
        ],
        (raw().replace('"openai"', '"openai", "bytes_per_token": NaN'), "bytes_per_token"),
    ],
)
def test_from_env_is_strict_and_never_echoes_values(monkeypatch, value, message):
    monkeypatch.setenv("MODEL_API", value)
    with pytest.raises(ValueError, match=message) as caught:
        model_api.from_env()
    assert "SENTINEL" not in str(caught.value) and caught.value.__cause__ is None


# 7. AgentCore is Bedrock-only ----------------------------------------------------------------


def test_agentcore_host_refuses_to_start_with_model_api(monkeypatch):
    server = MagicMock(side_effect=AssertionError("the server must not start"))
    monkeypatch.setattr(agentcore_host, "ThreadingHTTPServer", server)
    monkeypatch.setenv("MODEL_API", json.dumps(config()))
    with pytest.raises(SystemExit, match="MODEL_API"):
        agentcore_host.main()
    server.assert_not_called()
    monkeypatch.setenv("MODEL_API", "")  # empty means unset, as for the Lambda runtime
    server.side_effect = RuntimeError("started")
    with pytest.raises(RuntimeError, match="started"):
        agentcore_host.main()
