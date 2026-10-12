"""Bounded SigV4 AgentCore adapter. Never retry/fail over an ambiguous invocation."""

import json
import re
import subprocess
import sys
import time

from tracaus.runtime import RuntimeStop, sdk_client

MAX_WIRE_BYTES = 96000


def validate_target(arn, qualifier, region, account):
    if (
        not isinstance(arn, str)
        or not re.fullmatch(
            rf"arn:aws:bedrock-agentcore:{re.escape(region)}:{re.escape(account)}:runtime/[A-Za-z][A-Za-z0-9_]*-[A-Za-z0-9]{{10}}",
            arn,
        )
        or not isinstance(qualifier, str)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", qualifier)
        or qualifier == "DEFAULT"
    ):
        raise ValueError("AgentCore requires an explicit release endpoint in the intended account/region")


def invoke(payload, *, arn, qualifier, region, account, session_id, deadline, client=None):
    validate_target(arn, qualifier, region, account)
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{33,256}", session_id):
        raise ValueError("Invalid AgentCore session identifier")
    wire = json.dumps(payload).encode()
    if len(wire) > 96000 or deadline - time.time() < 25:
        raise RuntimeStop("REQUEST_LIMIT")
    args = dict(
        arn=arn, qualifier=qualifier, region=region, account=account, session_id=session_id, deadline=deadline
    )
    if client is None:
        # A killable process bounds SDK connection, trickling reads and buffering
        # together. Killing this client never implies cancellation at the service.
        output = run_child(
            [sys.executable, "-m", "tracaus.agentcore"],
            json.dumps({"payload": payload, **args}, ensure_ascii=False).encode(),
            deadline,
        )
        result = json.loads(output)
        validate_result(result, payload)
        return result
    return invoke_stream(payload, **args, client=client)


def run_child(command, wire, deadline):
    child = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    try:
        output, _ = child.communicate(wire, timeout=max(0.001, deadline - time.time()))
    except BaseException as exc:
        try:
            child.kill()
        except ProcessLookupError:
            pass
        child.communicate(timeout=3)
        if not isinstance(exc, Exception):
            raise
        raise RuntimeError("Remote execution interrupted; outcome may be ambiguous") from None
    if child.returncode != 0 or time.time() >= deadline or len(output) > 100000:
        raise RuntimeError("Remote execution failed or exceeded its deadline")
    return output


def invoke_stream(payload, *, arn, qualifier, region, account, session_id, deadline, client=None):
    wire = json.dumps(payload).encode()
    client = client or sdk_client("bedrock-agentcore", region, 15)
    response = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        qualifier=qualifier,
        runtimeSessionId=session_id,
        contentType="application/json",
        accept="text/event-stream",
        payload=wire,
    )
    stream = response.get("response")
    if stream is None:
        raise RuntimeError("Remote execution response missing")
    try:
        if (
            response.get("statusCode") != 200
            or response.get("contentType", "").split(";")[0] != "text/event-stream"
        ):
            raise RuntimeError("Remote execution response unsupported")
        size, lines, data = 0, 0, []
        for raw in bounded_lines(stream, deadline):
            size += len(raw) + 1
            lines += 1
            if time.time() >= deadline or size > MAX_WIRE_BYTES or lines > 512:
                raise RuntimeError("Remote execution limit reached")
            if raw.startswith(b"data: "):
                data.append(raw[6:])
            elif raw == b"" and data:
                event = json.loads(b"\n".join(data))
                data = []
                if event.get("event") == "error":
                    raise RuntimeError("Remote execution failed; inspect the durable incident")
                if event.get("event") == "result":
                    result = event["result"]
                    validate_result(result, payload)
                    return result
        raise RuntimeError("Remote execution ended without a result")
    finally:
        stream.close()


def bounded_lines(stream, deadline):
    pending, size = bytearray(), 0
    while True:
        if time.time() >= deadline:
            raise RuntimeError("Remote execution deadline reached")
        # StreamingBody.read(n) can wait for n bytes. Read one so a complete
        # small result is usable even if the upstream connection stays open.
        chunk = stream.read(1)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_WIRE_BYTES or time.time() >= deadline:
            raise RuntimeError("Remote execution limit reached")
        pending.extend(chunk)
        if b"\n" not in chunk:
            continue
        while (end := pending.find(b"\n")) != -1:
            yield bytes(pending[:end]).rstrip(b"\r")
            del pending[: end + 1]
    if pending:
        yield bytes(pending)


def validate_result(result, payload):
    if (
        not isinstance(result, dict)
        or type(result.get("version")) is not int
        or result["version"] != 1
        or result.get("release") != payload["release"]
        or type(result.get("complete")) is not bool
        or not isinstance(result.get("text"), str)
        or len(result["text"].encode()) > 64000
        or not isinstance(result.get("code"), str)
        or len(result["code"]) > 128
        or not isinstance(result.get("usage"), dict)
        or set(result["usage"]) != {"input_tokens", "output_tokens"}
        or any(type(v) is not int or v < 0 for v in result["usage"].values())
        or any(
            not isinstance(result.get(key), list)
            or any(
                not isinstance(v, str) or v not in {"fetch_logs", "fetch_metrics", "fetch_ecs"}
                for v in result[key]
            )
            or len(result[key]) != len(set(result[key]))
            for key in ("tools", "evidence")
        )
    ):
        raise RuntimeError("Remote result contract mismatch")


def main():
    try:
        request = json.loads(sys.stdin.buffer.read(102400))
        payload = request.pop("payload")
        validate_target(request["arn"], request["qualifier"], request["region"], request["account"])
        result = invoke_stream(payload, **request)
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
