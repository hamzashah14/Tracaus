"""Conditional DynamoDB ledger: accepted event, incident and intents commit together."""

import hashlib
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from random import SystemRandom

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError

from tracaus import safety
from tracaus.transport import dumps

SERIALIZE = TypeSerializer()
MAX_ATTEMPTS = 3
DATABASE_CONFIG = Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 1})


def item(value):
    return {key: SERIALIZE.serialize(field) for key, field in value.items()}


def conditional(exc):
    if not isinstance(exc, ClientError):
        return False
    code = exc.response.get("Error", {}).get("Code")
    if code == "ConditionalCheckFailedException":
        return True
    if code != "TransactionCanceledException":
        return False
    reasons = exc.response.get("CancellationReasons") or []
    return any(reason.get("Code") == "ConditionalCheckFailed" for reason in reasons) and all(
        reason.get("Code") in {"None", "ConditionalCheckFailed"} for reason in reasons
    )


class Ledger:
    def __init__(self, table_name, client=None, table=None, region_name=None):
        self.name = table_name
        self.client = client or boto3.client("dynamodb", region_name=region_name, config=DATABASE_CONFIG)
        self.table = table or boto3.resource(
            "dynamodb", region_name=region_name, config=DATABASE_CONFIG
        ).Table(table_name)

    def get(self, pk, sk="META"):
        return self.table.get_item(Key={"PK": pk, "SK": sk}, ConsistentRead=True).get("Item")

    def accept(self, event, retention_days, cooldown_minutes=0):
        """Store one source event: ACCEPTED, NON_ACTIONABLE, SUPPRESSED (see cooldown) or DUPLICATE."""
        for attempt in range(3):
            try:
                return self._accept(event, retention_days, cooldown_minutes)
            except ClientError as exc:
                if not conditional(exc):
                    raise
                if self.get(f"EVENT#{event['event_id']}"):
                    return "DUPLICATE"
                if attempt == 2:
                    raise
                # Another writer changed the cooldown marker or alarm state after we read it: decide again.

    def _accept(self, event, retention_days, cooldown_minutes):
        event.update(safety.bounded(event))
        for derived in ("out_of_order", "related_incident_id"):  # decided below, again on every retry
            event.pop(derived, None)
        eid, iid = event["event_id"], event["incident_id"]
        now_epoch = int(time.time())
        expires = now_epoch + retention_days * 86400
        previous, alarm_pk, recovery = None, None, None
        if event.get("track_recovery"):
            alarm_pk = "ALARM#" + hashlib.sha256(event["native_id"].encode()).hexdigest()
            previous = self.get(alarm_pk)
            if previous and datetime.fromisoformat(
                previous["occurred_at"].replace("Z", "+00:00")
            ) > datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00")):
                event["out_of_order"] = True
            elif (
                previous
                and event["state"] == "OK"
                and previous.get("last_incident_id")
                and previous.get("ttl", 0) > time.time()
            ):
                recovery = previous["last_incident_id"]
                parent = self.get(f"INCIDENT#{recovery}")
                if parent and parent.get("ttl", 0) > time.time():
                    event["related_incident_id"] = recovery
                else:
                    recovery = None
        # One open incident per target: while it is inside its cooldown, a later alarm (or a later ECS task
        # stop: a crash loop repeats) is stored and counted on that incident but starts no investigation and
        # sends no email. An EC2 stop is rare and always gets its own incident.
        marker_pk, parent, new_marker = f"COOLDOWN#{event['instance_id']}", None, False
        if cooldown_minutes and event["actionable"] and event.get("investigate", True):
            marker = self.get(marker_pk)
            if marker and int(marker["until_epoch"]) > now_epoch:
                parent = marker["incident_id"] if event["kind"] in {"alarm", "ecs"} else None
            else:
                new_marker = True
        event_row = {
            "PK": f"EVENT#{eid}",
            "SK": "META",
            "record_version": 1,
            "record_type": "event",
            "incident_id": iid,
            "event": event,
            "ttl": expires,
            **({"suppressed_by": parent} if parent else {}),
        }
        records = [
            {
                "Put": {
                    "TableName": self.name,
                    "Item": item(event_row),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            }
        ]
        if parent:
            records += [
                {
                    "ConditionCheck": {
                        "TableName": self.name,
                        "Key": item({"PK": marker_pk, "SK": "META"}),
                        "ConditionExpression": "incident_id=:parent AND until_epoch>:now",
                        "ExpressionAttributeValues": item({":parent": parent, ":now": now_epoch}),
                    }
                },
                {
                    "Update": {
                        "TableName": self.name,
                        "Key": item({"PK": f"INCIDENT#{parent}", "SK": "META"}),
                        "UpdateExpression": "ADD suppressed_alarms :one",
                        "ConditionExpression": "attribute_exists(PK)",
                        "ExpressionAttributeValues": item({":one": 1}),
                    }
                },
            ]
        elif event["actionable"]:
            deadline = (
                int(datetime.fromisoformat(event["received_at"].replace("Z", "+00:00")).timestamp()) + 600
            )
            incident = {
                "PK": f"INCIDENT#{iid}",
                "SK": "META",
                "record_version": 1,
                "record_type": "incident",
                "event_id": eid,
                "instance_id": event["instance_id"],
                "correlation_key": event["correlation_key"],
                "occurred_at": event["occurred_at"],
                "received_at": event["received_at"],
                "ingested_at": event.get("ingested_at", event["received_at"]),
                "status": "PENDING" if event.get("investigate", True) else "CANARY",
                "trigger_kind": event["kind"],
                "trigger_state": event["state"],
                "work_intent_sk": "INTENT#WORK#1",
                "work_due_epoch": deadline - 600,
                "attempts": 0,
                "fencing_token": 0,
                "tokens_reserved": 0,
                "model_steps": 0,
                "tool_calls": 0,
                "log_queries": 0,
                "deadline_epoch": deadline,
                "GSI3PK": "OPEN",
                "GSI3SK": f"{deadline:012d}#{iid}",
                "ttl": expires,
                **({"canary_slot": event["canary_slot"]} if "canary_slot" in event else {}),
            }
            if not event.get("investigate", True):
                for key in ("work_intent_sk", "work_due_epoch", "GSI3PK", "GSI3SK"):
                    incident.pop(key)
            records.append(
                {
                    "Put": {
                        "TableName": self.name,
                        "Item": item(incident),
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                }
            )
            for kind in ("INITIAL", "WORK"):
                if kind == "WORK" and not event.get("investigate", True):
                    continue
                records.append(
                    {
                        "Put": {
                            "TableName": self.name,
                            "Item": item(self.intent(iid, kind, event["received_at"], expires)),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    }
                )
            records.append(
                {
                    "Put": {
                        "TableName": self.name,
                        "Item": item(
                            {
                                "PK": f"INCIDENT#{iid}",
                                "SK": "NOTIFICATION#INITIAL",
                                "record_version": 1,
                                "record_type": "notification",
                                "status": "PENDING",
                                "attempts": 0,
                                "fencing_token": 0,
                                "ttl": expires,
                            }
                        ),
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                }
            )
            if new_marker:
                records.append(
                    {
                        "Put": {
                            "TableName": self.name,
                            "Item": item(
                                {
                                    "PK": marker_pk,
                                    "SK": "META",
                                    "record_type": "cooldown",
                                    "incident_id": iid,
                                    "until_epoch": now_epoch + cooldown_minutes * 60,
                                    "ttl": expires,
                                }
                            ),
                            "ConditionExpression": "attribute_not_exists(PK) OR until_epoch<=:now",
                            "ExpressionAttributeValues": item({":now": now_epoch}),
                        }
                    }
                )
        if alarm_pk and not event.get("out_of_order"):
            state = {
                "PK": alarm_pk,
                "SK": "META",
                "record_type": "alarm_state",
                "ttl": expires,
                "occurred_at": event["occurred_at"],
                "event_id": eid,
                "state": event["state"],
            }
            last = (parent or iid) if event["actionable"] else (previous or {}).get("last_incident_id")
            if last:
                state["last_incident_id"] = last
            put = {
                "TableName": self.name,
                "Item": item(state),
                "ConditionExpression": "event_id=:previous" if previous else "attribute_not_exists(PK)",
            }
            if previous:
                put["ExpressionAttributeValues"] = item({":previous": previous["event_id"]})
            records.append({"Put": put})
            if recovery:
                records.append(
                    {
                        "Update": {
                            "TableName": self.name,
                            "Key": item({"PK": f"INCIDENT#{recovery}", "SK": "META"}),
                            "UpdateExpression": "SET recovery_event_id=:event, recovered_at=:at",
                            "ConditionExpression": "attribute_exists(PK) AND #t>:now",
                            "ExpressionAttributeNames": {"#t": "ttl"},
                            "ExpressionAttributeValues": item(
                                {":event": eid, ":at": event["occurred_at"], ":now": int(time.time())}
                            ),
                        }
                    }
                )
        self.client.transact_write_items(TransactItems=records)
        if parent:
            return "SUPPRESSED"
        return "ACCEPTED" if event["actionable"] else "NON_ACTIONABLE"

    @staticmethod
    def intent(incident_id, kind, due_at, ttl, suffix="1"):
        return {
            "PK": f"INCIDENT#{incident_id}",
            "SK": f"INTENT#{kind}#{suffix}",
            "record_version": 1,
            "record_type": "intent",
            "kind": kind,
            "status": "PENDING",
            "due_at": due_at,
            "GSI1PK": "PENDING",
            "GSI1SK": f"{due_at}#{incident_id}#{kind}#{suffix}",
            "ttl": ttl,
        }

    def pending(self, now, limit=100, cursor=None):
        # A missed stream record is repaired by this eventually consistent index.
        result = self.table.query(
            IndexName="PendingIntents",
            KeyConditionExpression=Key("GSI1PK").eq("PENDING") & Key("GSI1SK").lte(f"{now}~"),
            Limit=limit,
            **({"ExclusiveStartKey": cursor} if cursor else {}),
        )
        return result.get("Items", []), result.get("LastEvaluatedKey")

    def dispatch(self, row, sqs, queue_url):
        if row.get("status") != "PENDING" or not row["SK"].startswith("INTENT#"):
            return "SKIPPED"
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if row["due_at"] > now:
            return "DEFERRED"  # The due-intent index and scheduled sweep honor retry backoff.
        # Sending before marking SENT permits duplicates but never loses the intent.
        sqs.send_message(QueueUrl=queue_url, MessageBody=f"{row['PK']}|{row['SK']}")
        try:
            self.table.update_item(
                Key={"PK": row["PK"], "SK": row["SK"]},
                UpdateExpression="SET #s=:sent REMOVE GSI1PK, GSI1SK",
                ConditionExpression="#s=:pending",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":sent": "SENT", ":pending": "PENDING"},
            )
        except ClientError as exc:
            if not conditional(exc):
                raise
        return "SENT"

    def claim(self, incident_id, owner, now_epoch, lease_seconds, intent_sk=None):
        pk = f"INCIDENT#{incident_id}"
        current = self.get(pk)
        if not current or current["status"] not in {"PENDING", "RETRY"}:
            return None  # Expired RUNNING attempts are fenced and closed by the reconciler.
        token = int(current["fencing_token"]) + 1
        intent_sk = intent_sk or current.get("work_intent_sk", "INTENT#WORK#1")
        attempt = {
            "PK": pk,
            "SK": f"ATTEMPT#{token}",
            "record_version": 1,
            "record_type": "attempt",
            "status": "RUNNING",
            "lease_owner": owner,
            "fencing_token": token,
            "started_epoch": now_epoch,
            "lease_until": now_epoch + lease_seconds,
            "ttl": current["ttl"],
        }
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.name,
                            "Key": item({"PK": pk, "SK": "META"}),
                            "UpdateExpression": "SET #s=:running, lease_owner=:owner, lease_until=:until, GSI2PK=:active, GSI2SK=:lease_key REMOVE GSI3PK, GSI3SK ADD attempts :one, fencing_token :one",
                            "ConditionExpression": "(#s=:pending OR #s=:retry) AND attempts<:max AND deadline_epoch>:now AND fencing_token=:previous AND work_intent_sk=:intent AND work_due_epoch<=:now",
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": item(
                                {
                                    ":running": "RUNNING",
                                    ":pending": "PENDING",
                                    ":retry": "RETRY",
                                    ":owner": owner,
                                    ":until": now_epoch + lease_seconds,
                                    ":now": now_epoch,
                                    ":one": 1,
                                    ":max": MAX_ATTEMPTS,
                                    ":active": "ACTIVE",
                                    ":lease_key": f"{now_epoch + lease_seconds:012d}#{incident_id}",
                                    ":previous": token - 1,
                                    ":intent": intent_sk,
                                }
                            ),
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.name,
                            "Item": item(attempt),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ]
            )
        except ClientError as exc:
            if conditional(exc):
                return None
            raise
        return {
            **current,
            "status": "RUNNING",
            "lease_owner": owner,
            "lease_until": now_epoch + lease_seconds,
            "attempts": int(current["attempts"]) + 1,
            "fencing_token": token,
        }

    def begin_execution(self, claim, policy_hash, release_hash=None):
        """One execution per fence, including duplicate remote invocation requests."""
        condition = (
            "#s=:running AND lease_owner=:owner AND fencing_token=:token AND lease_until>:now AND deadline_epoch>:now "
            "AND (attribute_not_exists(execution_fence) OR execution_fence<>:token) "
            "AND (attribute_not_exists(budget_policy_hash) OR budget_policy_hash=:policy)"
        )
        expression = "SET execution_fence=:token, budget_policy_hash=:policy"
        values = {
            ":running": "RUNNING",
            ":owner": claim["lease_owner"],
            ":token": claim["fencing_token"],
            ":now": int(time.time()),
            ":policy": policy_hash,
        }
        if release_hash:
            condition += " AND (attribute_not_exists(execution_release) OR execution_release=:release)"
            expression += ", execution_release=:release"
            values[":release"] = release_hash
        try:
            self.table.update_item(
                Key={"PK": claim["PK"], "SK": "META"},
                UpdateExpression=expression,
                ConditionExpression=condition,
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if conditional(exc):
                from tracaus.runtime import RuntimeStop

                raise RuntimeStop("STALE_OR_ALREADY_EXECUTING") from None
            raise

    def reserve(self, claim, limits, delta):
        """Never refund ambiguous reservations; enforce aggregate limits across attempts."""
        from tracaus.runtime import COUNTERS, RuntimeStop

        if (
            not delta
            or delta.keys() - set(COUNTERS)
            or any(type(v) is not int or v < 1 for v in delta.values())
        ):
            raise ValueError("Invalid budget reservation")
        if any(delta[key] > limits.caps[key] for key in delta):
            raise RuntimeStop("BUDGET_EXHAUSTED")
        names = {"#s": "status"}
        values = {
            ":running": "RUNNING",
            ":owner": claim["lease_owner"],
            ":token": claim["fencing_token"],
            ":now": int(time.time()),
            ":policy": limits.fingerprint,
        }
        conditions = [
            "#s=:running",
            "lease_owner=:owner",
            "fencing_token=:token",
            "execution_fence=:token",
            "lease_until>:now",
            "deadline_epoch>:now",
            "budget_policy_hash=:policy",
        ]
        additions = []
        for index, (key, amount) in enumerate(delta.items()):
            name = f"#b{index}"
            names[name] = key
            values[f":d{index}"] = amount
            values[f":c{index}"] = limits.caps[key] - amount
            conditions.append(f"{name}<=:c{index}")
            additions.append(f"{name} :d{index}")
        try:
            result = self.table.update_item(
                Key={"PK": claim["PK"], "SK": "META"},
                UpdateExpression="ADD " + ", ".join(additions),
                ConditionExpression=" AND ".join(conditions),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )["Attributes"]
        except ClientError as exc:
            if conditional(exc):
                raise RuntimeStop("BUDGET_OR_LEASE_EXHAUSTED") from None
            raise
        return {key: int(result[key]) for key in COUNTERS}

    def record_usage(self, claim, input_tokens, output_tokens):
        """Observed usage is a lower bound after lost responses; reservations stay authoritative."""
        if any(type(v) is not int or v < 0 for v in (input_tokens, output_tokens)):
            raise ValueError("Invalid observed token usage")
        self.table.update_item(
            Key={"PK": claim["PK"], "SK": "META"},
            UpdateExpression="ADD input_tokens_observed :input, output_tokens_observed :output",
            ConditionExpression="#s=:running AND lease_owner=:owner AND fencing_token=:token AND execution_fence=:token AND lease_until>:now AND deadline_epoch>:now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":running": "RUNNING",
                ":owner": claim["lease_owner"],
                ":token": claim["fencing_token"],
                ":now": int(time.time()),
                ":input": input_tokens,
                ":output": output_tokens,
            },
        )

    def attempt_update(self, claim, status, now_epoch):
        return {
            "Update": {
                "TableName": self.name,
                "Key": item({"PK": claim["PK"], "SK": f"ATTEMPT#{claim['fencing_token']}"}),
                "UpdateExpression": "SET #s=:next, ended_epoch=:now",
                "ConditionExpression": "#s=:running AND lease_owner=:owner",
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": item(
                    {
                        ":next": status,
                        ":now": now_epoch,
                        ":running": "RUNNING",
                        ":owner": claim["lease_owner"],
                    }
                ),
            }
        }

    def checkpoint(self, claim, report, version_id, now_epoch):
        evidence = {
            "PK": claim["PK"],
            "SK": f"EVIDENCE#{version_id}",
            "record_version": 1,
            "record_type": "evidence",
            "kind": "checkpoint",
            **report,
            "version_id": version_id,
            "ttl": claim["ttl"],
        }
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.name,
                            "Key": item({"PK": claim["PK"], "SK": "META"}),
                            "UpdateExpression": "SET checkpoint_version=:version",
                            "ConditionExpression": "#s=:running AND lease_owner=:owner AND fencing_token=:token AND lease_until>=:now AND deadline_epoch>=:now",
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": item(
                                {
                                    ":version": version_id,
                                    ":running": "RUNNING",
                                    ":owner": claim["lease_owner"],
                                    ":token": claim["fencing_token"],
                                    ":now": now_epoch,
                                }
                            ),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.name,
                            "Key": item({"PK": claim["PK"], "SK": f"ATTEMPT#{claim['fencing_token']}"}),
                            "UpdateExpression": "SET checkpoint_version=:version",
                            "ConditionExpression": "#s=:running AND lease_owner=:owner",
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": item(
                                {
                                    ":version": version_id,
                                    ":running": "RUNNING",
                                    ":owner": claim["lease_owner"],
                                }
                            ),
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.name,
                            "Item": item(evidence),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ]
            )
        except ClientError as exc:
            if conditional(exc):
                return "STALE"
            raise
        return "CHECKPOINTED"

    def complete(self, claim, report, version_id, status, now_epoch):
        pk = claim["PK"]
        iid = pk.removeprefix("INCIDENT#")
        evidence = {
            "PK": pk,
            "SK": f"EVIDENCE#{version_id}",
            "record_version": 1,
            "record_type": "evidence",
            "bucket": report["bucket"],
            "key": report["key"],
            "version_id": version_id,
            "sha256": report["sha256"],
            "classification": report["classification"],
            "ttl": claim["ttl"],
        }
        notification = {
            "PK": pk,
            "SK": "NOTIFICATION#REPORT",
            "record_version": 1,
            "record_type": "notification",
            "status": "PENDING",
            "attempts": 0,
            "fencing_token": 0,
            "ttl": claim["ttl"],
        }
        due_at = datetime.fromtimestamp(now_epoch, timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            self.client.transact_write_items(
                TransactItems=[
                    self.attempt_update(claim, status, now_epoch),
                    {
                        "Update": {
                            "TableName": self.name,
                            "Key": item({"PK": pk, "SK": "META"}),
                            "UpdateExpression": "SET #s=:done, report_version=:version REMOVE lease_owner, lease_until, GSI2PK, GSI2SK, GSI3PK, GSI3SK",
                            "ConditionExpression": "#s=:running AND lease_owner=:owner AND fencing_token=:token AND lease_until>=:now AND deadline_epoch>=:now",
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": item(
                                {
                                    ":done": status,
                                    ":version": version_id,
                                    ":running": "RUNNING",
                                    ":owner": claim["lease_owner"],
                                    ":token": claim["fencing_token"],
                                    ":now": now_epoch,
                                }
                            ),
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.name,
                            "Item": item(evidence),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.name,
                            "Item": item(notification),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.name,
                            "Item": item(self.intent(iid, "REPORT", due_at, claim["ttl"])),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ]
            )
        except ClientError as exc:
            if conditional(exc):
                return "STALE"
            raise
        return "COMPLETED"

    def retry(self, claim):
        iid = claim["PK"].removeprefix("INCIDENT#")
        now = datetime.now(timezone.utc)
        delay = SystemRandom().randint(1, min(60, 5 * 2 ** int(claim["attempts"])))
        due = (now + timedelta(seconds=delay)).isoformat().replace("+00:00", "Z")
        try:
            self.client.transact_write_items(
                TransactItems=[
                    self.attempt_update(claim, "RETRY", int(now.timestamp())),
                    {
                        "Update": {
                            "TableName": self.name,
                            "Key": item({"PK": claim["PK"], "SK": "META"}),
                            "UpdateExpression": "SET #s=:retry, GSI3PK=:open, GSI3SK=:deadline_key, work_intent_sk=:intent, work_due_epoch=:due REMOVE lease_owner, lease_until, GSI2PK, GSI2SK",
                            "ConditionExpression": "#s=:running AND lease_owner=:owner AND fencing_token=:token",
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": item(
                                {
                                    ":retry": "RETRY",
                                    ":running": "RUNNING",
                                    ":owner": claim["lease_owner"],
                                    ":token": claim["fencing_token"],
                                    ":open": "OPEN",
                                    ":deadline_key": f"{int(claim['deadline_epoch']):012d}#{iid}",
                                    ":intent": f"INTENT#WORK#{claim['attempts'] + 1}",
                                    ":due": int((now + timedelta(seconds=delay)).timestamp()),
                                }
                            ),
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.name,
                            "Item": item(
                                self.intent(iid, "WORK", due, claim["ttl"], str(claim["attempts"] + 1))
                            ),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ]
            )
        except ClientError as exc:
            if conditional(exc):
                return "STALE"
            raise
        return "RETRY"

    def expired(self, now_epoch, limit=100, cursor=None):
        result = self.table.query(
            IndexName="ActiveLeases",
            KeyConditionExpression=Key("GSI2PK").eq("ACTIVE") & Key("GSI2SK").lte(f"{now_epoch:012d}~"),
            Limit=limit,
            **({"ExclusiveStartKey": cursor} if cursor else {}),
        )
        return result.get("Items", []), result.get("LastEvaluatedKey")

    def overdue(self, now_epoch, limit=100, cursor=None):
        result = self.table.query(
            IndexName="DueIncidents",
            KeyConditionExpression=Key("GSI3PK").eq("OPEN") & Key("GSI3SK").lte(f"{now_epoch:012d}~"),
            Limit=limit,
            **({"ExclusiveStartKey": cursor} if cursor else {}),
        )
        return result.get("Items", []), result.get("LastEvaluatedKey")

    def sweep_state(self, kind):
        row = self.get(f"SWEEP#{kind}") or {}
        return {"revision": int(row.get("revision", 0)), "cursor": row.get("cursor")}

    def save_sweep(self, kind, previous, cursor):
        """CAS progress: concurrent sweeps can repeat rows but cannot overwrite newer progress."""
        try:
            self.table.update_item(
                Key={"PK": f"SWEEP#{kind}", "SK": "META"},
                UpdateExpression="SET revision=:next, #c=:cursor, #t=:ttl",
                ConditionExpression="attribute_not_exists(revision) OR revision=:previous",
                ExpressionAttributeNames={"#c": "cursor", "#t": "ttl"},
                ExpressionAttributeValues={
                    ":next": previous + 1,
                    ":previous": previous,
                    ":cursor": cursor,
                    ":ttl": int(time.time()) + 30 * 86400,
                },
            )
        except ClientError as exc:
            if conditional(exc):
                return "STALE"
            raise
        return "SAVED"

    def degrade_overdue(self, incident, now_epoch):
        """Give queued work an explicit outcome when its incident deadline passes."""
        if incident.get("status") not in {"PENDING", "RETRY"} or incident["deadline_epoch"] > now_epoch:
            return "UNCHANGED"
        pk = incident["PK"]
        iid = pk.removeprefix("INCIDENT#")
        due = datetime.fromtimestamp(now_epoch, timezone.utc).isoformat().replace("+00:00", "Z")
        notification = {
            "PK": pk,
            "SK": "NOTIFICATION#REPORT",
            "record_version": 1,
            "record_type": "notification",
            "status": "PENDING",
            "attempts": 0,
            "fencing_token": 0,
            "ttl": incident["ttl"],
        }
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.name,
                            "Key": item({"PK": pk, "SK": "META"}),
                            "UpdateExpression": "SET #s=:degraded REMOVE GSI3PK, GSI3SK",
                            "ConditionExpression": "(#s=:pending OR #s=:retry) AND deadline_epoch<=:now AND fencing_token=:token",
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": item(
                                {
                                    ":degraded": "DEGRADED",
                                    ":pending": "PENDING",
                                    ":retry": "RETRY",
                                    ":now": now_epoch,
                                    ":token": incident["fencing_token"],
                                }
                            ),
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.name,
                            "Item": item(notification),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.name,
                            "Item": item(self.intent(iid, "REPORT", due, incident["ttl"], "overdue")),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ]
            )
        except ClientError as exc:
            if conditional(exc):
                return "STALE"
            raise
        return "DEGRADED"

    def recover(self, claim, now_epoch):
        """Fence the expired owner, then create a fresh work intent or visible failure."""
        if claim.get("status") != "RUNNING" or claim.get("lease_until", now_epoch + 1) >= now_epoch:
            return "UNCHANGED"
        pk = claim["PK"]
        iid = pk.removeprefix("INCIDENT#")
        terminal = claim["attempts"] >= MAX_ATTEMPTS or claim["deadline_epoch"] <= now_epoch
        due = datetime.fromtimestamp(now_epoch, timezone.utc).isoformat().replace("+00:00", "Z")
        kind = "REPORT" if terminal else "WORK"
        suffix = "timeout" if terminal else str(claim["attempts"] + 1)
        records = [
            self.attempt_update(claim, "EXPIRED", now_epoch),
            {
                "Update": {
                    "TableName": self.name,
                    "Key": item({"PK": pk, "SK": "META"}),
                    "UpdateExpression": (
                        "SET #s=:next REMOVE lease_owner, lease_until, GSI2PK, GSI2SK, GSI3PK, GSI3SK"
                        if terminal
                        else "SET #s=:next, GSI3PK=:open, GSI3SK=:deadline_key, work_intent_sk=:intent, work_due_epoch=:due REMOVE lease_owner, lease_until, GSI2PK, GSI2SK"
                    ),
                    "ConditionExpression": "#s=:running AND lease_owner=:owner AND fencing_token=:token AND lease_until<:now",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": item(
                        {
                            ":next": "DEGRADED" if terminal else "RETRY",
                            ":running": "RUNNING",
                            ":owner": claim["lease_owner"],
                            ":token": claim["fencing_token"],
                            ":now": now_epoch,
                            **(
                                {}
                                if terminal
                                else {
                                    ":open": "OPEN",
                                    ":deadline_key": f"{int(claim['deadline_epoch']):012d}#{iid}",
                                    ":intent": f"INTENT#WORK#{suffix}",
                                    ":due": now_epoch,
                                }
                            ),
                        }
                    ),
                }
            },
            {
                "Put": {
                    "TableName": self.name,
                    "Item": item(self.intent(iid, kind, due, claim["ttl"], suffix)),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
        ]
        if terminal:
            records.append(
                {
                    "Put": {
                        "TableName": self.name,
                        "Item": item(
                            {
                                "PK": pk,
                                "SK": "NOTIFICATION#REPORT",
                                "record_version": 1,
                                "record_type": "notification",
                                "status": "PENDING",
                                "attempts": 0,
                                "fencing_token": 0,
                                "ttl": claim["ttl"],
                            }
                        ),
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                }
            )
        try:
            self.client.transact_write_items(TransactItems=records)
        except ClientError as exc:
            if conditional(exc):
                return "STALE"
            raise
        return "DEGRADED" if terminal else "RETRY"

    def claim_notification(self, incident_id, kind, owner, now_epoch):
        key = {"PK": f"INCIDENT#{incident_id}", "SK": f"NOTIFICATION#{kind}"}
        try:
            return self.table.update_item(
                Key=key,
                UpdateExpression="SET #s=:sending, lease_owner=:owner, lease_until=:until, GSI2PK=:active, GSI2SK=:lease_key ADD attempts :one, fencing_token :one",
                ConditionExpression="(#s=:pending OR (#s=:sending AND lease_until<:now)) AND attempts<:max",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":sending": "SENDING",
                    ":pending": "PENDING",
                    ":owner": owner,
                    ":until": now_epoch + 60,
                    ":active": "NOTIFY",
                    ":lease_key": f"{now_epoch + 60:012d}#{incident_id}#{kind}",
                    ":now": now_epoch,
                    ":one": 1,
                    ":max": MAX_ATTEMPTS,
                },
                ReturnValues="ALL_NEW",
            )["Attributes"]
        except ClientError as exc:
            if conditional(exc):
                return None
            raise

    def notification_result(self, claim, status, message_id=None):
        values = {
            ":next": status,
            ":sending": "SENDING",
            ":owner": claim["lease_owner"],
            ":token": claim["fencing_token"],
        }
        update = "SET #s=:next"
        if message_id:
            update += ", publisher_message_id=:message_id"
            values[":message_id"] = message_id
            values[":messages"] = {message_id}
        update += " REMOVE lease_owner, lease_until, GSI2PK, GSI2SK"
        if message_id:
            update += " ADD publisher_message_ids :messages"
        try:
            self.table.update_item(
                Key={"PK": claim["PK"], "SK": claim["SK"]},
                UpdateExpression=update,
                ConditionExpression="#s=:sending AND lease_owner=:owner AND fencing_token=:token",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if not conditional(exc):
                raise

    def expired_notifications(self, now_epoch, limit=100, cursor=None):
        result = self.table.query(
            IndexName="ActiveLeases",
            KeyConditionExpression=Key("GSI2PK").eq("NOTIFY") & Key("GSI2SK").lte(f"{now_epoch:012d}~"),
            Limit=limit,
            **({"ExclusiveStartKey": cursor} if cursor else {}),
        )
        return result.get("Items", []), result.get("LastEvaluatedKey")

    def recover_notification(self, row, now_epoch):
        if row.get("status") != "SENDING" or row.get("lease_until", now_epoch) >= now_epoch:
            return "UNCHANGED"
        terminal = row["attempts"] >= MAX_ATTEMPTS
        kind = row["SK"].removeprefix("NOTIFICATION#")
        if kind not in {"INITIAL", "REPORT"}:
            raise ValueError("Invalid notification kind")
        records = [
            {
                "Update": {
                    "TableName": self.name,
                    "Key": item({"PK": row["PK"], "SK": row["SK"]}),
                    "UpdateExpression": "SET #s=:next REMOVE lease_owner, lease_until, GSI2PK, GSI2SK ADD fencing_token :one",
                    "ConditionExpression": "#s=:sending AND lease_owner=:owner AND fencing_token=:token AND lease_until<:now",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": item(
                        {
                            ":next": "AMBIGUOUS" if terminal else "PENDING",
                            ":sending": "SENDING",
                            ":owner": row["lease_owner"],
                            ":token": row["fencing_token"],
                            ":now": now_epoch,
                            ":one": 1,
                        }
                    ),
                }
            }
        ]
        if not terminal:
            due = datetime.fromtimestamp(now_epoch, timezone.utc).isoformat().replace("+00:00", "Z")
            records.append(
                {
                    "Put": {
                        "TableName": self.name,
                        "Item": item(
                            self.intent(
                                row["PK"].removeprefix("INCIDENT#"), kind, due, row["ttl"], uuid.uuid4().hex
                            )
                        ),
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                }
            )
        try:
            self.client.transact_write_items(TransactItems=records)
        except ClientError as exc:
            if conditional(exc):
                return "STALE"
            raise
        return "AMBIGUOUS" if terminal else "RETRY"

    def notification_replay_plan(self, incident_id, kind):
        if not re.fullmatch(r"[0-9a-f]{32}", incident_id) or kind not in {"INITIAL", "REPORT"}:
            raise ValueError("Invalid notification reference")
        pk = "INCIDENT#" + incident_id
        incident, row = self.get(pk), self.get(pk, "NOTIFICATION#" + kind)
        now = int(time.time())
        if (
            not incident
            or incident.get("status") in {"DELETING", "DELETED"}
            or not row
            or min(incident.get("ttl", 0), row.get("ttl", 0)) <= now
            or row["status"] not in {"FAILED", "AMBIGUOUS"}
            or row.get("replay_runs", 0) >= 2
        ):
            raise ValueError(
                "Only unexpired failed/ambiguous notifications with remaining replay allowance can be replayed"
            )
        plan = {
            "incident_id": incident_id,
            "kind": kind,
            "status": row["status"],
            "attempts": int(row["attempts"]),
            "fencing_token": int(row["fencing_token"]),
            "replay_runs": int(row.get("replay_runs", 0)),
            "ttl": int(row["ttl"]),
        }
        plan["review_hash"] = hashlib.sha256(dumps(plan).encode()).hexdigest()
        return plan

    def replay_notification(self, reviewed, operator):
        current = self.notification_replay_plan(reviewed["incident_id"], reviewed["kind"])
        if current != reviewed:
            raise ValueError("Notification replay plan changed; inspect again")
        pk, kind, nonce = "INCIDENT#" + current["incident_id"], current["kind"], uuid.uuid4().hex
        now = int(time.time())
        due = datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")
        self.client.transact_write_items(
            TransactItems=[
                {
                    "ConditionCheck": {
                        "TableName": self.name,
                        "Key": item({"PK": pk, "SK": "META"}),
                        "ConditionExpression": "attribute_exists(PK) AND #s<>:deleting AND #s<>:deleted",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": item({":deleting": "DELETING", ":deleted": "DELETED"}),
                    }
                },
                {
                    "Update": {
                        "TableName": self.name,
                        "Key": item({"PK": pk, "SK": "NOTIFICATION#" + kind}),
                        "UpdateExpression": "SET #s=:pending, attempts=:zero ADD fencing_token :one, replay_runs :one",
                        "ConditionExpression": "#s=:old AND attempts=:attempts AND fencing_token=:fence AND #t>:now AND (replay_runs=:runs OR (attribute_not_exists(replay_runs) AND :runs=:zero))",
                        "ExpressionAttributeNames": {"#s": "status", "#t": "ttl"},
                        "ExpressionAttributeValues": item(
                            {
                                ":pending": "PENDING",
                                ":old": current["status"],
                                ":attempts": current["attempts"],
                                ":fence": current["fencing_token"],
                                ":now": now,
                                ":runs": current["replay_runs"],
                                ":zero": 0,
                                ":one": 1,
                            }
                        ),
                    }
                },
                {
                    "Put": {
                        "TableName": self.name,
                        "Item": item(self.intent(current["incident_id"], kind, due, current["ttl"], nonce)),
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                },
                {
                    "Put": {
                        "TableName": self.name,
                        "Item": item(
                            {
                                "PK": pk,
                                "SK": "NOTIFICATION_REPLAY#" + nonce,
                                "operator": operator,
                                "kind": kind,
                                "created_at": due,
                                "review_hash": current["review_hash"],
                                "record_type": "notification_replay",
                                "record_version": 1,
                                "ttl": current["ttl"],
                            }
                        ),
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                },
            ]
        )
        return {"status": "NOTIFICATION_REPLAY_RECORDED", "incident_id": current["incident_id"], "kind": kind}

    def replay_plan(self, event_id):
        event = self.get(f"EVENT#{event_id}")
        if not event:
            raise ValueError("Accepted event not found")
        incident = self.get(f"INCIDENT#{event['incident_id']}")
        if (
            not incident
            or incident["status"] not in {"PENDING", "RETRY"}
            or incident["attempts"] >= MAX_ATTEMPTS
            or incident.get("deadline_epoch", int(time.time()) + 1) <= int(time.time())
        ):
            raise ValueError("Completed, active or exhausted investigation cannot be replayed")
        review = {
            "event_id": event_id,
            "incident_id": event["incident_id"],
            "status": incident["status"],
            "attempts": int(incident["attempts"]),
            "fencing_token": int(incident["fencing_token"]),
            "work_intent_sk": incident.get("work_intent_sk", "INTENT#WORK#1"),
        }
        import hashlib

        review["review_hash"] = hashlib.sha256(dumps(review).encode()).hexdigest()
        return review

    def replay(self, reviewed, operator):
        current = self.replay_plan(reviewed["event_id"])
        if current != reviewed:
            raise ValueError("Replay plan changed; review current incident state")
        iid = current["incident_id"]
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        nonce = uuid.uuid4().hex
        source = self.get(f"EVENT#{current['event_id']}")
        audit = {
            "PK": f"INCIDENT#{iid}",
            "SK": f"REPLAY#{now}#{nonce}",
            "record_version": 1,
            "record_type": "replay",
            "operator": operator,
            "event_id": current["event_id"],
            "review_hash": reviewed["review_hash"],
            "created_at": now,
            "ttl": source["ttl"],
        }
        self.client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": self.name,
                        "Key": item({"PK": f"INCIDENT#{iid}", "SK": "META"}),
                        "UpdateExpression": "SET work_intent_sk=:next_intent, work_due_epoch=:due",
                        "ConditionExpression": "(#s=:pending OR #s=:retry) AND attempts=:attempts AND fencing_token=:token AND work_intent_sk=:previous_intent",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": item(
                            {
                                ":pending": "PENDING",
                                ":retry": "RETRY",
                                ":attempts": current["attempts"],
                                ":token": current["fencing_token"],
                                ":previous_intent": current["work_intent_sk"],
                                ":next_intent": f"INTENT#WORK#{nonce}",
                                ":due": int(time.time()),
                            }
                        ),
                    }
                },
                {
                    "Put": {
                        "TableName": self.name,
                        "Item": item(audit),
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                },
                {
                    "Put": {
                        "TableName": self.name,
                        "Item": item(self.intent(iid, "WORK", now, source["ttl"], nonce)),
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                },
            ]
        )
        return {
            "status": "REPLAY_INTENT_RECORDED",
            "incident_id": iid,
            "review_hash": reviewed["review_hash"],
        }
