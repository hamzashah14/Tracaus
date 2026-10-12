"""SQS ingress: commit the source event and first intents atomically."""

from tracaus import pipeline


def lambda_handler(event, context):
    return pipeline.ingest(event, context)
