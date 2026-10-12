"""Publish an independent minimal initial alert."""

from tracaus import pipeline


def lambda_handler(event, context):
    return pipeline.initial(event, context)
