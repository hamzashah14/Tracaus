"""Reviewed terminal-incident erasure; tombstones prevent resurrection and replay.

Customer backup/restore exclusion is mandatory: erasing live versions cannot
erase historical PITR snapshots. No background deletion or automatic retry.
"""

import hashlib
import json
import re
import time
from decimal import Decimal

from boto3.dynamodb.conditions import Key

TERMINAL = {"COMPLETE", "DEGRADED", "CANARY", "DELETING", "DELETED"}
NOTIFY_TERMINAL = {"PUBLISHER_ACCEPTED", "FAILED", "AMBIGUOUS", "SUPPRESSED"}
GRACE = 900
MAX_ROWS = 10000


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def integer(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, Decimal) and value.is_finite() and value >= 0 and value == value.to_integral_value():
        return int(value)
    raise ValueError("Malformed incident time or fence")


class Erasure:
    def __init__(self, table, s3, bucket, account, *, clock=time.time):
        self.table, self.s3, self.bucket, self.account, self.clock = table, s3, bucket, account, clock

    def rows(self, pk):
        rows, after = [], None
        while True:
            kw = {"KeyConditionExpression": Key("PK").eq(pk), "ConsistentRead": True}
            if after:
                kw["ExclusiveStartKey"] = after
            response = self.table.query(**kw)
            rows.extend(response.get("Items", []))
            if len(rows) > MAX_ROWS:
                raise ValueError("Erasure requires a bounded incident manifest")
            after = response.get("LastEvaluatedKey")
            if not after:
                return rows

    def versions(self, iid):
        versions, key, version = [], None, None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": f"incidents/{iid}/", "ExpectedBucketOwner": self.account}
            if key:
                kw.update(KeyMarker=key)
                if version:
                    kw["VersionIdMarker"] = version
            response = self.s3.list_object_versions(**kw)
            versions.extend(
                {"Key": v["Key"], "VersionId": v["VersionId"]}
                for kind in ("Versions", "DeleteMarkers")
                for v in response.get(kind, [])
            )
            if any(
                not v["Key"].startswith(f"incidents/{iid}/") or not v["VersionId"] or v["VersionId"] == "null"
                for v in versions
            ):
                raise ValueError("Unversioned or foreign object is outside the erasure manifest")
            if len(versions) > MAX_ROWS:
                raise ValueError("Erasure requires a bounded object manifest")
            if not response.get("IsTruncated"):
                return sorted(versions, key=lambda v: (v["Key"], v["VersionId"]))
            key, version = response["NextKeyMarker"], response.get("NextVersionIdMarker", "")

    def plan(self, iid):
        if not re.fullmatch(r"[0-9a-f]{32}", iid):
            raise ValueError("Invalid incident")
        rows = self.rows("INCIDENT#" + iid)
        meta = next((r for r in rows if r["SK"] == "META"), None)
        now = int(self.clock())
        if (
            not meta
            or meta.get("status") not in TERMINAL
            or now < max(integer(meta.get("deadline_epoch")), integer(meta.get("lease_until", 0))) + GRACE
        ):
            raise ValueError("Only quiescent terminal incidents can be erased")
        if any(r["SK"].startswith("NOTIFICATION#") and r.get("status") not in NOTIFY_TERMINAL for r in rows):
            raise ValueError("Finish or suppress pending delivery before erasure")
        integer(meta.get("fencing_token"))
        eid = meta.get("event_id")
        if eid is not None and not re.fullmatch(r"[0-9a-f]{64}", eid):
            raise ValueError("Malformed incident event link")
        event = (
            self.table.get_item(Key={"PK": "EVENT#" + eid, "SK": "META"}, ConsistentRead=True).get("Item")
            if eid
            else None
        )
        value = {
            "version": 1,
            "incident_id": iid,
            "bucket": self.bucket,
            "account": self.account,
            "meta": {
                "status": meta["status"],
                "fencing_token": integer(meta["fencing_token"]),
                "deadline_epoch": integer(meta["deadline_epoch"]),
                "event_id": eid,
            },
            "rows": sorted(
                [{"PK": r["PK"], "SK": r["SK"], "digest": digest(r)} for r in rows], key=lambda r: r["SK"]
            ),
            "event_digest": digest(event),
            "objects": self.versions(iid),
        }
        return {**value, "review_hash": digest(value)}

    def apply(self, reviewed):
        iid = reviewed["incident_id"]
        if self.plan(iid) != reviewed:
            raise ValueError("Erasure plan changed; review again")
        pk, meta, now = "INCIDENT#" + iid, reviewed["meta"], int(self.clock())
        # Compare status and execution fence against concurrent operator changes.
        self.table.update_item(
            Key={"PK": pk, "SK": "META"},
            UpdateExpression="SET #s=:deleting, purge_ref=:ref ADD fencing_token :one REMOVE report_version, checkpoint_version, GSI1PK, GSI1SK, GSI2PK, GSI2SK, GSI3PK, GSI3SK",
            ConditionExpression="#s=:before AND fencing_token=:fence",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":deleting": "DELETING",
                ":before": meta["status"],
                ":fence": meta["fencing_token"],
                ":ref": reviewed["review_hash"],
                ":one": 1,
            },
        )
        # Include only explicitly reviewed immutable version IDs, including markers.
        objects = reviewed["objects"]
        for start in range(0, len(objects), 1000):
            response = self.s3.delete_objects(
                Bucket=self.bucket,
                ExpectedBucketOwner=self.account,
                Delete={"Objects": objects[start : start + 1000], "Quiet": True},
            )
            if response.get("Errors"):
                raise ValueError("Erasure partially failed; retain tombstone and review retry")
        if self.versions(iid):
            raise ValueError("Unreviewed object versions appeared; retain tombstone")
        for row in reviewed["rows"]:
            if row["SK"] != "META":
                self.table.delete_item(Key={k: row[k] for k in ("PK", "SK")})
        # Retain minimal anti-replay metadata through the 35-day PITR window.
        ttl = now + 35 * 86400
        eid = meta["event_id"]
        if eid:
            self.table.put_item(
                Item={"PK": "EVENT#" + eid, "SK": "META", "incident_id": iid, "deleted": True, "ttl": ttl},
                ConditionExpression="attribute_not_exists(PK) OR incident_id=:iid",
                ExpressionAttributeValues={":iid": iid},
            )
        self.table.put_item(
            Item={
                "PK": pk,
                "SK": "META",
                "status": "DELETED",
                "event_id": eid,
                "deadline_epoch": meta["deadline_epoch"],
                "fencing_token": int(meta["fencing_token"]) + 1,
                "purge_ref": reviewed["review_hash"],
                "ttl": ttl,
            },
            ConditionExpression="#s=:deleting AND purge_ref=:ref",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":deleting": "DELETING", ":ref": reviewed["review_hash"]},
        )
        return {"status": "DELETED", "incident_id": iid, "restore_exclusion_required": True}
