"""Default chat request limits. The diagnostics evaluation reads them."""

from dataclasses import asdict

from tracaus.runtime import Limits

DEFAULT = {
    "chat_limits": asdict(
        Limits(tokens_reserved=24000, model_steps=6, tool_calls=6, log_queries=12, output_tokens=1024)
    ),
}
