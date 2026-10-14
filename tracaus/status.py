"""Authorized, bounded incident status lookup for the customer web UI."""

import hashlib
import os
import re
import time

import boto3
from botocore.config import Config

from tracaus import safety

INCIDENT = re.compile(r"[0-9a-f]{32}\Z")
READ_CONFIG = Config(connect_timeout=3, read_timeout=8, retries={"total_max_attempts": 2})


def load(incident_id, *, allowed=None):
    if not isinstance(incident_id, str) or not INCIDENT.fullmatch(incident_id):
        raise ValueError("Invalid incident reference")
    table_name = os.getenv("INCIDENT_TABLE")
    region = os.getenv("MONITOR_REGION")
    if not table_name or not region:
        raise ValueError("Incident status storage is not configured")
    table = boto3.resource("dynamodb", region_name=region, config=READ_CONFIG).Table(table_name)
    pk = f"INCIDENT#{incident_id}"
    incident = table.get_item(Key={"PK": pk, "SK": "META"}, ConsistentRead=True).get("Item")
    if (
        incident is None
        or incident.get("status") in {"DELETING", "DELETED"}
        or int(incident.get("ttl", 0)) <= time.time()
    ):
        return None
    if allowed is not None and incident.get("instance_id") not in allowed:
        # Same answer as a missing incident: do not reveal what exists outside the caller's scope.
        return None
    result = {
        key: incident.get(key)
        for key in (
            "instance_id",
            "occurred_at",
            "received_at",
            "status",
            "attempts",
            "suppressed_alarms",
            "recovered_at",
            "recovery_event_id",
        )
    }
    result["incident_id"] = incident_id
    if incident.get("suppressed_names"):
        result["suppressed_alarm_names"] = sorted(incident["suppressed_names"])
    version = incident.get("report_version") or incident.get("checkpoint_version")
    if version:
        evidence = table.get_item(Key={"PK": pk, "SK": f"EVIDENCE#{version}"}, ConsistentRead=True).get(
            "Item"
        )
        if (
            evidence
            and int(evidence.get("ttl", 0)) > time.time()
            and evidence.get("bucket") == os.getenv("REPORT_BUCKET")
            and evidence.get("version_id") == version
            and evidence.get("key", "").startswith(f"incidents/{incident_id}/")
        ):
            response = boto3.client("s3", region_name=region, config=READ_CONFIG).get_object(
                Bucket=evidence["bucket"],
                Key=evidence["key"],
                VersionId=evidence["version_id"],
                **(
                    {"ExpectedBucketOwner": os.environ["EXPECTED_ACCOUNT_ID"]}
                    if os.getenv("EXPECTED_ACCOUNT_ID")
                    else {}
                ),
            )
            stream = response["Body"]
            try:
                data = stream.read(65537)
            finally:
                stream.close()
            if len(data) > 65536:
                raise ValueError("Stored report exceeds the UI read limit")
            if hashlib.sha256(data).hexdigest() != evidence.get("sha256"):
                raise ValueError("Stored report checksum differs from its evidence record")
            result["report"] = safety.text(data.decode("utf-8", errors="replace"))
            result["partial"] = not bool(incident.get("report_version"))
    return result
