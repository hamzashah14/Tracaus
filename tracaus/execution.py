"""Shared execution service for direct calls and the AgentCore HTTP host."""

import json
import math
import os
import re
import time

from tracaus.runtime import LambdaTools, Limits, MemoryBudget, RuntimeStop, run
from tracaus.telemetry import correlate


def required(name):
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing execution setting: {name}")
    return value


def limits():
    return Limits(**json.loads(required("RUNTIME_LIMITS")))


def tools(policy, reserve, instance=None, anchor=None, allowed=None, access_guard=None, local=None):
    # Development only: this process runs the tool handlers with the caller's AWS credentials.
    if local is not None:
        return local.tools(
            required("BEDROCK_REGION"),
            required("EXPECTED_ACCOUNT_ID"),
            {instance} if instance else (allowed if allowed is not None else set(local.instances)),
            policy,
            reserve,
            anchor,
            access_guard,
        )
    return LambdaTools(
        required("BEDROCK_REGION"),
        required("EXPECTED_ACCOUNT_ID"),
        {"fetch_logs": required("LOGS_TOOL_ARN"), "fetch_metrics": required("METRICS_TOOL_ARN")},
        {instance}
        if instance
        else (allowed if allowed is not None else set(required("ALLOWED_INSTANCE_IDS").split(","))),
        policy,
        reserve,
        anchor,
        access_guard=access_guard,
    )


def execute(payload, *, store=None, checkpoint=None, local=None, allowed=None, progress=None):
    """The host derives all authorization and allowances from trusted configuration.

    `local` (a tracaus.local_tools.LocalConfig) is passed only by the development chat path, never by hosts.
    `allowed` (a frozenset of instance IDs) is passed only by team mode chat; hosts never pass it.
    `progress` (a callable taking a step name) is passed only by the in-process chat path."""
    if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
        raise RuntimeStop("INVALID_EXECUTION_REQUEST")
    release = "local" if local is not None else required("RUNTIME_RELEASE")
    if payload.get("release") != release:
        raise RuntimeStop("RELEASE_MISMATCH")
    policy = limits()
    mode = payload.get("mode")
    purpose = os.getenv("EXECUTION_PURPOSE", "both")
    if (
        mode not in {"chat", "incident"}
        or (purpose != "both" and purpose != mode)
        or (allowed is not None and mode != "chat")
        or (
            local is not None and (mode != "chat" or os.getenv("ENVIRONMENT", "development") != "development")
        )
    ):
        raise RuntimeStop("PURPOSE_MISMATCH")
    if mode == "incident":
        if set(payload) != {"version", "release", "mode", "incident_id", "owner", "fence", "deadline"}:
            raise RuntimeStop("INVALID_EXECUTION_REQUEST")
        if (
            not isinstance(payload["incident_id"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", payload["incident_id"])
            or type(payload["fence"]) is not int
            or payload["fence"] < 1
            or not isinstance(payload["owner"], str)
            or not re.fullmatch(r"[0-9a-f-]{36}", payload["owner"])
            or type(payload["deadline"]) not in (int, float)
            or not math.isfinite(payload["deadline"])
        ):
            raise RuntimeStop("INVALID_EXECUTION_REQUEST")
        from tracaus.pipeline import checkpoint_writer, ledger

        store = store or ledger()
        claim = store.get("INCIDENT#" + payload["incident_id"])
        if (
            not claim
            or claim["status"] != "RUNNING"
            or claim["lease_owner"] != payload["owner"]
            or int(claim["fencing_token"]) != payload["fence"]
        ):
            raise RuntimeStop("STALE_EXECUTION")
        deadline = min(
            payload["deadline"],
            int(claim["lease_until"]) - 50,
            int(claim["deadline_epoch"]) - 60,
            time.time() + 400,
        )
        if deadline - time.time() < 30:
            raise RuntimeStop("DEADLINE")
        source = store.get("EVENT#" + claim["event_id"])
        if not source or not source["event"]["actionable"]:
            raise RuntimeStop("SOURCE_UNAVAILABLE")
        event = source["event"]
        if event["instance_id"] not in required("ALLOWED_INSTANCE_IDS").split(","):
            raise RuntimeStop("UNAUTHORIZED_INSTANCE")
        store.begin_execution(claim, policy.fingerprint, release)

        def reserve(delta):
            return store.reserve(claim, policy, delta)

        executor = tools(policy, reserve, event["instance_id"], event["occurred_at"])
        checkpoint = checkpoint or checkpoint_writer(store, claim)
        prompt = (
            f"Investigate instance {event['instance_id']} near {event['occurred_at']}. "
            f"Trigger: {event['kind']} {event['state']}. Use logs and metrics. "
            f"Window allowance: {policy.window_minutes} minutes per side. "
            "Return evidence, uncertainty, remediation and follow-up."
        )
        with correlate(payload["incident_id"], payload["fence"]):
            result = run(
                prompt,
                model_id=required("BEDROCK_MODEL_ID"),
                region=required("BEDROCK_REGION"),
                tools=executor,
                reserve=reserve,
                limits=policy,
                deadline=deadline,
                checkpoint=checkpoint,
                require_evidence=True,
                record_usage=lambda input_tokens, output_tokens: store.record_usage(
                    claim, input_tokens, output_tokens
                ),
            )
    elif mode == "chat":
        if set(payload) != {"version", "release", "mode", "prompt", "history"}:
            raise RuntimeStop("INVALID_EXECUTION_REQUEST")
        if not isinstance(payload["prompt"], str) or not 1 <= len(payload["prompt"].strip()) <= 4000:
            raise RuntimeStop("INVALID_PROMPT")
        history = payload["history"]
        if (
            not isinstance(history, list)
            or len(history) > 24
            or any(
                not isinstance(m, dict)
                or set(m) != {"role", "content"}
                or m["role"] not in {"user", "assistant"}
                or not isinstance(m["content"], str)
                or len(m["content"].encode()) > 32000
                for m in history
            )
        ):
            raise RuntimeStop("INVALID_HISTORY")
        budget = MemoryBudget(policy)

        def reserve(delta):
            return budget.reserve(delta)

        result = run(
            payload["prompt"],
            model_id=required("BEDROCK_MODEL_ID"),
            region=required("BEDROCK_REGION"),
            tools=tools(
                policy,
                reserve,
                allowed=allowed,
                **({"local": local} if local is not None else {}),
            ),
            reserve=reserve,
            limits=policy,
            deadline=time.time() + 180,
            history=history,
            progress=progress,
        )
    else:
        raise RuntimeStop("INVALID_EXECUTION_MODE")
    return {**result, "release": release}


def incident_request(claim, seconds):
    return {
        "version": 1,
        "release": required("RUNTIME_RELEASE"),
        "mode": "incident",
        "incident_id": claim["PK"].removeprefix("INCIDENT#"),
        "owner": claim["lease_owner"],
        "fence": int(claim["fencing_token"]),
        "deadline": time.time() + seconds,
    }


def safe_chat(executor, payload):
    """Expected execution denials cross adapters without private exceptions."""
    try:
        return executor(payload)
    except RuntimeStop as exc:
        code = str(exc)
        allowed = {
            "RELEASE_MISMATCH",
            "PURPOSE_MISMATCH",
            "INVALID_EXECUTION_REQUEST",
            "INVALID_HISTORY",
            "INVALID_PROMPT",
            "DEADLINE",
        }
        if code not in allowed:
            code = "EXECUTION_STOPPED"
        return {
            "version": 1,
            "release": required("RUNTIME_RELEASE"),
            "complete": False,
            "code": code,
            "text": "No validated diagnosis; access or execution allowance could not be verified. Operator review required.",
            "tools": [],
            "evidence": [],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
