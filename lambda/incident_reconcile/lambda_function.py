"""Repair missed streams and expired leases."""

from tracaus import pipeline


def lambda_handler(event, context):
    return pipeline.reconcile(event, context)
