"""Claim and fence investigation work; save redacted versioned evidence."""

import os

from tracaus import agentcore, execution, pipeline


def lambda_handler(event, context):
    if os.getenv("EXECUTION_PURPOSE") == "chat":
        if set(event) != {"runtime_chat"} or event["runtime_chat"].get("mode") != "chat":
            raise ValueError("Dedicated gateway accepts only chat")
        payload = event["runtime_chat"]
        if execution.required("RUNTIME_TARGET") == "standalone":
            return execution.safe_chat(execution.execute, payload)
        import time
        import uuid

        return agentcore.invoke(
            payload,
            arn=execution.required("AGENTCORE_RUNTIME_ARN"),
            qualifier=execution.required("AGENTCORE_ENDPOINT"),
            region=execution.required("BEDROCK_REGION"),
            account=execution.required("EXPECTED_ACCOUNT_ID"),
            session_id="chat_" + str(uuid.uuid4()),
            deadline=time.time() + min(180, context.get_remaining_time_in_millis() / 1000 - 30),
        )
    if set(event) == {"runtime_canary"}:
        if os.getenv("ALLOW_RUNTIME_CANARY") != "true" or event["runtime_canary"].get("mode") != "chat":
            raise ValueError("Runtime canary is staging-only")
        if execution.required("RUNTIME_TARGET") == "standalone":
            return execution.execute(event["runtime_canary"])
        import time
        import uuid

        return agentcore.invoke(
            event["runtime_canary"],
            arn=execution.required("AGENTCORE_RUNTIME_ARN"),
            qualifier=execution.required("AGENTCORE_ENDPOINT"),
            region=execution.required("BEDROCK_REGION"),
            account=execution.required("EXPECTED_ACCOUNT_ID"),
            session_id="canary_" + str(uuid.uuid4()),
            deadline=time.time() + min(180, context.get_remaining_time_in_millis() / 1000 - 30),
        )
    return pipeline.work(event, context)
