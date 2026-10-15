"""Durable incident handlers. Each SQS record acknowledges only after its durable handoff."""

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.config import Config

from tracaus import safety
from tracaus.incident import InvalidEvent, OutOfInventory, normalize_sns
from tracaus.ledger import MAX_ATTEMPTS, Ledger
from tracaus.telemetry import emit
from tracaus.time import iso_utc, parse_utc
from tracaus.transport import clip_utf8

CLIENT_CONFIG = Config(connect_timeout=3, read_timeout=8, retries={"total_max_attempts": 1})
REPORT_LIMIT = 64 * 1024
STATUS_LINK = re.compile(r"https://[^\s#?]+\Z")


def env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Required pipeline setting is missing: {name}")
    return value


def clients(service):
    return boto3.client(service, region_name=env("MONITOR_REGION"), config=CLIENT_CONFIG)


def ledger():
    return Ledger(env("INCIDENT_TABLE"), region_name=env("MONITOR_REGION"))


def partial_batch(event, handler, stage="ingress"):
    failures = []
    for record in event.get("Records", []):
        try:
            handler(record)
        except Exception:
            # No customer payload or exception text in logs. SQS retries only this message.
            failures.append({"itemIdentifier": record["messageId"]})
            emit(stage, "HANDOFF_FAILED", metrics={"Failure": 1})
    return {"batchItemFailures": failures}


def ingest(event, context=None):
    store = ledger()
    allowed = set(env("ALLOWED_INSTANCE_IDS").split(","))
    topic = env("ALARMS_TOPIC_ARN")
    account = env("EXPECTED_ACCOUNT_ID")
    region = env("MONITOR_REGION")
    prefix = env("ALARM_NAME_PREFIX")
    retention = int(env("INCIDENT_RETENTION_DAYS"))
    ecs_targets = json.loads(os.getenv("ECS_TARGETS") or "{}")

    def one(record):
        try:
            source = normalize_sns(
                record["body"],
                topic,
                account,
                region,
                allowed,
                prefix,
                canary_topic=os.getenv("CANARY_TOPIC_ARN"),
                track_recovery=os.getenv("TRACK_ALARM_RECOVERY") == "true",
                ecs=ecs_targets,
            )
            source["ingested_at"] = source["received_at"]
            sent = datetime.fromtimestamp(int(record["attributes"]["SentTimestamp"]) / 1000, timezone.utc)
            if sent.timestamp() > time.time() + 5:
                raise InvalidEvent("Ingress timestamp is ahead of the receiver")
            if source.get("sns_timestamp"):
                sent = min(sent, parse_utc(source["sns_timestamp"]))
            source["received_at"] = iso_utc(sent)
        except OutOfInventory:
            emit(
                "ingress", "IGNORED", metrics={"Ignored": 1}
            )  # expected, not poison: do not retry it into the DLQ
            return
        except InvalidEvent:
            # Poison payload remains in the queue until its redrive DLQ. It is never
            # logged or silently acknowledged as an accepted incident.
            raise
        result = store.accept(source, retention, int(os.getenv("INCIDENT_COOLDOWN_MINUTES", "15")))
        emit(
            "ingress",
            result,
            incident_id=source["incident_id"],
            metrics={
                {"DUPLICATE": "Duplicate", "SUPPRESSED": "Suppressed"}.get(result, "Accepted"): 1,
                "QueueDelaySeconds": max(0, time.time() - parse_utc(source["received_at"]).timestamp()),
            },
        )

    return partial_batch(event, one)


def parse_intent(body):
    if not isinstance(body, str) or not re.fullmatch(
        r"INCIDENT#[0-9a-f]{32}\|INTENT#(?:INITIAL|WORK|REPORT)#[A-Za-z0-9]+", body
    ):
        raise ValueError("Invalid queue intent")
    pk, sk = body.split("|", 1)
    return pk, sk


def dispatch_row(store, row, sqs):
    queues = {
        "INITIAL": env("INITIAL_QUEUE_URL"),
        "WORK": env("WORK_QUEUE_URL"),
        "REPORT": env("REPORT_QUEUE_URL"),
    }
    kind = row["kind"]
    if kind not in queues:
        raise ValueError("Unknown intent kind")
    result = store.dispatch(row, sqs, queues[kind])
    emit("dispatch", result, incident_id=row["PK"].removeprefix("INCIDENT#"))
    return result


def dispatch(event, context=None):
    store = ledger()
    sqs = clients("sqs")
    failures = []
    # DynamoDB Streams partial failure identifiers are sequence numbers, not event IDs.
    for record in event.get("Records", []):
        sequence = record["dynamodb"]["SequenceNumber"]
        try:
            keys = record["dynamodb"]["Keys"]
            pk, sk = keys["PK"]["S"], keys["SK"]["S"]
            if not sk.startswith("INTENT#"):
                continue
            row = store.get(pk, sk)
            if row and row.get("status") == "PENDING":
                dispatch_row(store, row, sqs)
        except Exception:
            failures.append({"itemIdentifier": sequence})
            emit("dispatch", "HANDOFF_FAILED", metrics={"Failure": 1})
            break  # Later stream records must replay from the first failed sequence.
    return {"batchItemFailures": failures}


def reconcile(event=None, context=None):
    store = ledger()
    now = datetime.now(timezone.utc)
    now_text = now.isoformat().replace("+00:00", "Z")
    sqs = clients("sqs")
    failures = []
    deadline = time.monotonic() + min(
        50, context.get_remaining_time_in_millis() / 1000 - 10 if context else 50
    )

    def scan(kind, number, query, value, process, stop):
        state = store.sweep_state(kind)
        cursor, checked = state["cursor"], 0
        for _ in range(10):
            if time.monotonic() + 38 >= stop:
                failures.append("sweep deadline")
                break
            try:
                rows, cursor = query(value, cursor=cursor)
            except Exception:
                failures.append("index read")
                break
            for row in rows:
                if time.monotonic() + 30 >= stop:
                    failures.append("sweep deadline")
                    break
                progress = {key: row[key] for key in ("PK", "SK", f"GSI{number}PK", f"GSI{number}SK")}
                # Advance before a potentially killed/poisoned operation. Durable rows
                # remain eligible on the next wrap; a failed prefix cannot trap progress.
                if store.save_sweep(kind, state["revision"], progress) == "STALE":
                    return checked
                state["revision"] += 1
                try:
                    process(row)
                except Exception:
                    failures.append("handoff")  # Continue independent incident recovery.
                checked += 1
            else:
                if not cursor:
                    store.save_sweep(kind, state["revision"], None)
                    break
                continue
            break
        else:
            failures.append("sweep page limit")
        return checked

    def current_action(row, action):
        current = store.get(row["PK"], row.get("SK", "META"))
        if current:
            action(current, int(now.timestamp()))

    jobs = {
        "expired": (2, store.expired, int(now.timestamp()), lambda row: current_action(row, store.recover)),
        "overdue": (
            3,
            store.overdue,
            int(now.timestamp()),
            lambda row: current_action(row, store.degrade_overdue),
        ),
        "pending": (1, store.pending, now_text, lambda row: dispatch_row(store, row, sqs)),
        "notifications": (
            2,
            store.expired_notifications,
            int(now.timestamp()),
            lambda row: current_action(row, store.recover_notification),
        ),
    }
    mode = (event or {}).get("sweep", "pending")
    if mode not in jobs:
        raise ValueError("Unknown recovery scan")
    result = {"sweep": mode, "checked": scan(mode, *jobs[mode], deadline)}
    emit(
        "reconcile",
        "INCOMPLETE" if failures else "COMPLETE",
        metrics={
            "Heartbeat": 1,
            "SweepPending": result["checked"] if mode == "pending" else 0,
            "Failure": len(failures),
        },
    )
    if failures:
        raise RuntimeError("Reconciliation incomplete; due state remains durable for the next sweep")
    return result


def redact(text):
    if not isinstance(text, str):
        return "No usable investigation response."
    value = safety.text(text)
    return clip_utf8(value, REPORT_LIMIT, "\n[truncated]")


def work(event, context):
    store = ledger()
    bucket = env("REPORT_BUCKET")
    key_arn = env("REPORT_KMS_KEY_ARN")
    s3 = clients("s3")

    def one(record):
        pk, sk = parse_intent(record["body"])
        if not sk.startswith("INTENT#WORK#"):
            raise ValueError("Work queue received a different intent")
        iid = pk.removeprefix("INCIDENT#")
        current = store.get(pk)
        if current is None:
            raise RuntimeError("Accepted incident is unavailable")
        if (
            current.get("status") in {"COMPLETE", "DEGRADED", "DELETING", "DELETED"}
            or current.get("work_intent_sk") != sk
        ):
            return
        owner = str(uuid.uuid4())
        now = int(time.time())
        remaining = context.get_remaining_time_in_millis() // 1000 if context else 480
        budget = min(400, remaining - 60, int(current["deadline_epoch"]) - now - 60)
        if budget < 25:
            raise RuntimeError("Insufficient worker deadline")
        claim = store.claim(iid, owner, now, budget + 50, sk)
        if claim is None:
            return  # Another valid lease owns work; reconciler handles abandoned leases.
        emit("work", "CLAIMED", incident_id=iid, fence=claim["fencing_token"], metrics={"Attempt": 1})
        source = store.get(f"EVENT#{claim['event_id']}")
        if source is None:
            raise RuntimeError("Accepted source event is unavailable")
        incident = source["event"]

        checkpoint = checkpoint_writer(store, claim, s3)

        try:
            model_budget = min(budget, claim["lease_until"] - int(time.time()) - 50)
            if model_budget < 25:
                store.retry(claim)
                return
            answer, complete = invoke_agent(incident, model_budget, checkpoint, store=store, claim=claim)
            status = "COMPLETE" if complete and answer.strip() else "DEGRADED"
        except Exception:
            emit(
                "work", "MODEL_FAILED", incident_id=iid, fence=claim["fencing_token"], metrics={"Failure": 1}
            )
            if os.getenv("RUNTIME_TARGET", "standalone") == "agentcore":
                raise  # Remote execution may still own the fence; wait for external lease recovery.
            if claim["attempts"] < MAX_ATTEMPTS:
                store.retry(claim)
                return  # A new durable intent drives the next attempt.
            answer, status = "Automatic investigation failed; operator review required.", "DEGRADED"
        report = redact(answer)
        data = report.encode("utf-8")
        key = f"incidents/{iid}/reports/{claim['fencing_token']}.txt"
        uploaded = s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ServerSideEncryption="aws:kms",
            SSEKMSKeyId=key_arn,
            ContentType="text/plain; charset=utf-8",
        )
        version = uploaded.get("VersionId")
        if not version:
            raise RuntimeError("Versioned report write was not confirmed")
        stored = store.complete(
            claim,
            {
                "bucket": bucket,
                "key": key,
                "sha256": hashlib.sha256(data).hexdigest(),
                "classification": safety.VERSION,
            },
            version,
            status,
            int(time.time()),
        )
        if stored == "STALE":
            return  # A newer fencing token owns the incident; old object is orphaned for lifecycle cleanup.
        emit("work", status, incident_id=iid, fence=claim["fencing_token"], metrics={"ReportPersisted": 1})

    return partial_batch(event, one, "work")


def checkpoint_writer(store, claim, s3=None):
    bucket = env("REPORT_BUCKET")
    key_arn = env("REPORT_KMS_KEY_ARN")
    iid = claim["PK"].removeprefix("INCIDENT#")
    s3 = s3 or clients("s3")

    def checkpoint(answer):
        data = redact(answer).encode("utf-8")
        key = f"incidents/{iid}/checkpoints/{claim['fencing_token']}.txt"
        uploaded = s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ServerSideEncryption="aws:kms",
            SSEKMSKeyId=key_arn,
            ContentType="text/plain; charset=utf-8",
        )
        version = uploaded.get("VersionId")
        if not version:
            raise RuntimeError("Checkpoint version was not confirmed")
        stored = store.checkpoint(
            claim,
            {
                "bucket": bucket,
                "key": key,
                "sha256": hashlib.sha256(data).hexdigest(),
                "classification": safety.VERSION,
            },
            version,
            int(time.time()),
        )
        if stored != "CHECKPOINTED":
            raise RuntimeError("Checkpoint lease is stale")

    return checkpoint


def invoke_agent(incident, budget, checkpoint=None, *, store=None, claim=None):
    from tracaus import agentcore, execution

    target = env("RUNTIME_TARGET")
    if target not in {"standalone", "agentcore"}:
        raise ValueError("Unsupported runtime target")
    payload = execution.incident_request(claim, budget)
    if target == "standalone":
        result = execution.execute(payload, store=store, checkpoint=checkpoint)
    elif target == "agentcore":
        result = agentcore.invoke(
            payload,
            arn=env("AGENTCORE_RUNTIME_ARN"),
            qualifier=env("AGENTCORE_ENDPOINT"),
            region=env("BEDROCK_REGION"),
            account=env("EXPECTED_ACCOUNT_ID"),
            session_id=f"incident_{payload['incident_id']}_{payload['fence']}_{payload['owner']}",
            deadline=payload["deadline"],
        )
    else:
        raise ValueError("Unsupported runtime target")
    answer = result["text"]
    if not result["complete"]:
        answer += "\nInvestigation incomplete; operator review required."
    return answer, result["complete"]


def notify(event, context, kind):
    store = ledger()
    sns = clients("sns")
    topic = env("REPORTS_TOPIC_ARN")
    base = env("STATUS_BASE_URL").rstrip("/")
    if not STATUS_LINK.fullmatch(base):
        raise RuntimeError("Status base URL must be a fixed HTTPS path")

    def one(record):
        if context and context.get_remaining_time_in_millis() < 45000:
            raise RuntimeError("Insufficient notification time; retain this record for retry")
        pk, sk = parse_intent(record["body"])
        if not sk.startswith(f"INTENT#{kind}#"):
            raise ValueError("Notification queue received the wrong intent")
        iid = pk.removeprefix("INCIDENT#")
        notification = store.get(pk, f"NOTIFICATION#{kind}")
        if notification is None:
            raise RuntimeError("Notification record missing after durable handoff")
        if notification["status"] == "PUBLISHER_ACCEPTED":
            return
        if notification["status"] == "SENDING" and notification.get("lease_until", 0) < int(time.time()):
            store.recover_notification(notification, int(time.time()))
            raise RuntimeError("Expired notification lease recovered; retry the durable handoff")
        if notification["attempts"] >= MAX_ATTEMPTS:
            raise RuntimeError("Notification exhausted retries; retain in DLQ")
        claim = store.claim_notification(iid, kind, str(uuid.uuid4()), int(time.time()))
        if claim is None:
            raise RuntimeError("Notification has another active or expired lease; retry this queue record")
        incident = store.get(pk)
        if incident is None:
            raise RuntimeError("Notification incident missing")
        if incident.get("status") in {"DELETING", "DELETED"}:
            return
        link = f"{base}?incident={iid}"
        notification_id = f"{iid}-{kind.lower()}"
        if kind == "INITIAL":
            text = (
                f"Incident: {iid}\nInstance: {incident['instance_id']}\n"
                f"Trigger time: {incident['occurred_at']}\nStatus: {incident['status']}\n"
                f"Trigger: {incident.get('trigger_kind', 'unknown')} {incident.get('trigger_state', 'unknown')}\n"
                f"Status link: {link}\nNotification ID: {notification_id}"
            )
            if "canary_slot" in incident:
                text = "Scheduled delivery test; no application outage is asserted.\n" + text
        else:
            report_note = (
                "Full report is stored in the private evidence bucket; sign in to read it."
                if incident.get("report_version")
                else "No investigation report was completed; operator review is required."
            )
            text = (
                f"Incident: {iid}\nInstance: {incident['instance_id']}\n"
                f"Status: {incident['status']}\nStatus link: {link}\n"
                f"Notification ID: {notification_id}\n"
                f"{report_note}"
            )
            if incident.get("suppressed_names"):  # alarms folded into this incident, so none goes unseen
                text += "\nAlso alarmed during this incident: " + ", ".join(
                    sorted(incident["suppressed_names"])
                )
        try:
            response = sns.publish(
                TopicArn=topic,
                Subject=f"[Tracaus] {kind.title()} {iid[:12]}",
                Message=text,
                MessageAttributes={
                    "tracaus_notification": {"DataType": "String", "StringValue": notification_id},
                    "tracaus_canary": {
                        "DataType": "String",
                        "StringValue": "true" if "canary_slot" in incident else "false",
                    },
                    "tracaus_incident": {"DataType": "String", "StringValue": iid},
                },
            )
            if not response.get("MessageId"):
                raise RuntimeError("SNS did not acknowledge publication")
        except Exception:
            store.notification_result(claim, "FAILED" if claim["attempts"] >= MAX_ATTEMPTS else "PENDING")
            raise
        # A crash after publish can repeat email. The stable ID lets recipients
        # recognize duplicates; SNS acceptance never proves inbox delivery.
        store.notification_result(claim, "PUBLISHER_ACCEPTED", response["MessageId"])
        emit(
            kind.lower(),
            "PUBLISHER_ACCEPTED",
            incident_id=iid,
            fence=claim["fencing_token"],
            metrics={"NotificationAccepted": 1},
        )

    return partial_batch(event, one, kind.lower())


def initial(event, context=None):
    return notify(event, context, "INITIAL")


def report(event, context=None):
    return notify(event, context, "REPORT")
