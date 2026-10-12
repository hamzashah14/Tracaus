"""Customer-owned observation observation entrypoint."""

from tracaus.observability import canary_sender


def lambda_handler(event, context):
    return canary_sender(event, context)
