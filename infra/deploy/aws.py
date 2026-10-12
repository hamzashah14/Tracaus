"""Shared scoped AWS client construction for explicit operator commands."""

import boto3
from botocore.config import Config


def clients(service, region):
    return boto3.client(
        service,
        region_name=region,
        config=Config(
            connect_timeout=5, read_timeout=30, retries={"total_max_attempts": 2, "mode": "standard"}
        ),
    )
