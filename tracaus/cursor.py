"""Authenticated, expiring continuation bound to the deployment and instance."""

import base64
import hashlib
import hmac
import json
import os
import time
from functools import lru_cache

import boto3
from botocore.config import Config

from tracaus.transport import dumps


def scope(instance_id, prefix, region):
    return {
        "instance": instance_id,
        "prefix": prefix,
        "region": region,
        "environment": os.getenv("ENVIRONMENT", "development"),
        "account": os.getenv("EXPECTED_ACCOUNT_ID", "local"),
    }


@lru_cache(maxsize=8)
def secret_version(arn, version):
    if not version or not arn.startswith("arn:aws:secretsmanager:"):
        raise ValueError("A pinned cursor secret ARN and version are required.")
    client = boto3.client(
        "secretsmanager",
        region_name=arn.split(":")[3],
        config=Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 2}),
    )
    return client.get_secret_value(SecretId=arn, VersionId=version)["SecretString"]


def key():
    secret = os.getenv("LOG_CURSOR_SECRET", "")
    if os.getenv("LOG_CURSOR_SECRET_ARN"):
        secret = secret_version(
            os.environ["LOG_CURSOR_SECRET_ARN"], os.getenv("LOG_CURSOR_SECRET_VERSION", "")
        )
    if len(secret.encode()) < 32:
        raise ValueError("LOG_CURSOR_SECRET must contain at least 32 bytes to paginate discovery.")
    return secret.encode()


def encode(next_token, expected_scope):
    payload = dumps(
        {"v": 1, "scope": expected_scope, "next": next_token, "expires": int(time.time()) + 3600}
    ).encode()
    signature = hmac.new(key(), payload, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(signature + payload).decode()
    if len(token) > 6000:
        raise ValueError("CloudWatch continuation exceeds the supported cursor limit.")
    return token


def decode(token, expected_scope):
    try:
        if not isinstance(token, str) or len(token) > 6000:
            raise ValueError()
        raw = base64.b64decode(token.encode(), altchars=b"-_", validate=True)
        signature, payload = raw[:32], raw[32:]
        if not hmac.compare_digest(signature, hmac.new(key(), payload, hashlib.sha256).digest()):
            raise ValueError()
        value = json.loads(payload)
        if value["v"] != 1 or value["scope"] != expected_scope or value["expires"] < time.time():
            raise ValueError()
        if not isinstance(value["next"], str) or not value["next"]:
            raise ValueError()
        return value["next"]
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise ValueError("Continuation token is invalid, expired, or belongs to another scope.") from exc
