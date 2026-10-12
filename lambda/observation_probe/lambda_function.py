"""Customer-owned observation observation entrypoint."""

from tracaus.observability import observer


def lambda_handler(event, context):
    return observer(event, context)
