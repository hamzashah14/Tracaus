"""Bounded Bedrock chat requests with safe failures and recoverable partial text."""

import time
from dataclasses import dataclass, field

from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError

from tracaus import safety
from tracaus.runtime import RuntimeStop
from tracaus.transport import clip_utf8, error_result

MAX_PROMPT_CHARS = 4000
MAX_OUTPUT_BYTES = 32_000
MAX_REQUEST_SECONDS = 180
MAX_HISTORY_MESSAGES = 24
MAX_REQUESTS_PER_HOUR = 20
SESSION_SECONDS = 1800


@dataclass(frozen=True)
class ChatResult:
    text: str
    status: str
    code: str = ""
    message: str = ""
    reference: str = ""
    usage: dict = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "text", safety.text(self.text))


def failure(code, message, partial=""):
    problem = error_result(code, message)
    return ChatResult(partial, "partial" if partial else "error", code, message, problem["request_id"])


def invoke(prompt, session_id, settings, *, history=(), allowed=None, progress=None):
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
        return failure("INVALID_PROMPT", f"Enter a question of 1–{MAX_PROMPT_CHARS} characters.")
    if settings.problems():
        return failure(
            "NOT_CONFIGURED", "Complete the agent connection settings before starting an investigation."
        )
    if allowed is not None and settings.runtime_target != "standalone":
        # Only the in-process path enforces a scope; refuse rather than silently widen access.
        return failure("SCOPE_UNSUPPORTED", "This deployment cannot restrict chat to a list of instances.")
    text = ""
    try:
        from tracaus import agentcore, execution

        payload = {
            "version": 1,
            "release": "local" if settings.local_tools else settings.runtime_release,
            "mode": "chat",
            "prompt": prompt.strip(),
            "history": [
                {"role": m["role"], "content": m["content"]}
                for m in history[-MAX_HISTORY_MESSAGES:]
                if m.get("content")
            ],
        }
        if settings.runtime_target == "standalone":
            options = {}
            if settings.local_tools:
                from tracaus import local_tools

                options["local"] = local_tools.load(settings.local_tools)
            if allowed is not None:
                options["allowed"] = frozenset(allowed)
            if progress is not None:
                options["progress"] = progress
            result = execution.execute(payload, **options)
        else:
            result = agentcore.invoke(
                payload,
                arn=settings.agentcore_arn,
                qualifier=settings.agentcore_endpoint,
                region=settings.region,
                account=settings.account_id,
                session_id="chat_" + session_id,
                deadline=time.time() + MAX_REQUEST_SECONDS,
            )
        text = clip_utf8(result["text"], MAX_OUTPUT_BYTES)
        if result["complete"]:
            if len(result["text"].encode()) > MAX_OUTPUT_BYTES:
                return failure(
                    "OUTPUT_LIMIT",
                    "The validated report exceeds the display budget. Narrow the investigation.",
                    text,
                )
            return ChatResult(text, "ok", usage=result.get("usage") or {})
        return failure(
            "INVESTIGATION_INCOMPLETE",
            "Investigation stopped with incomplete evidence or an execution limit.",
            text,
        )
    except RuntimeStop:
        return failure("INVESTIGATION_INCOMPLETE", "Investigation stopped at an execution limit.")
    except (NoCredentialsError, PartialCredentialsError):
        return failure(
            "CREDENTIALS_UNAVAILABLE",
            "AWS credentials are unavailable. Sign in to the intended profile or check the workload role.",
            text,
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {
            "ExpiredToken",
            "ExpiredTokenException",
            "UnrecognizedClientException",
            "InvalidClientTokenId",
        }:
            return failure(
                "CREDENTIALS_EXPIRED",
                "AWS credentials are no longer valid. Refresh your sign-in and retry.",
                text,
            )
        if code in {"AccessDeniedException", "UnauthorizedException"}:
            return failure(
                "ACCESS_DENIED",
                "The configured identity cannot invoke this agent or its model. Check the role and model access.",
                text,
            )
        return failure(
            "AGENT_UNAVAILABLE",
            "The agent is unavailable. Check the region, runtime, model access and service status.",
            text,
        )
    except Exception:
        return failure(
            "REQUEST_FAILED",
            "The investigation connection failed. Any received text is preserved; you can retry.",
            text,
        )


def recent_attempts(attempts, now):
    return [stamp for stamp in attempts if now - stamp < 3600]


def append_exchange(messages, prompt, result, seconds=None):
    return (
        messages
        + [
            {"role": "user", "content": safety.text(prompt)},
            {
                "role": "assistant",
                "content": result.text,
                "status": result.status,
                "code": result.code,
                "message": result.message,
                "reference": result.reference,
                "usage": result.usage,
                "seconds": seconds,
            },
        ]
    )[-MAX_HISTORY_MESSAGES:]
