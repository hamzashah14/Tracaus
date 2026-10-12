"""Publish a follow-up reference without exposing private report content."""

from tracaus import pipeline


def lambda_handler(event, context):
    return pipeline.report(event, context)
