"""Customer-owned observation observation entrypoint."""

from tracaus.observability import recipient


def lambda_handler(event, context):
    return recipient(event, context)
