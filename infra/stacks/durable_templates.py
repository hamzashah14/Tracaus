"""Customer-owned durable pipeline foundation, immutable candidate and gated routing templates."""

import json

from infra.spec import (
    digest,
    ecs_ingress,
    ecs_services,
    event_rules,
    fallback_recipients,
    log_prefix,
    name,
    prefix,
    targets,
    topic_arn,
)
from infra.stacks.templates import (
    add_function,
    att,
    ref,
    resource,
    role,
    service_routing,
    statement,
    tagged,
    template,
)

FUNCTIONS = (
    "incident_ingress",
    "incident_dispatch",
    "incident_investigate",
    "incident_initial",
    "incident_report",
    "incident_reconcile",
)
QUEUES = ("Ingress", "Work", "Initial", "Report")


def queue_name(spec, key, dlq=False):
    return name(spec, f"{key.lower()}-{'dead' if dlq else 'queue'}")


def queue_arn(spec, key, dlq=False):
    return f"arn:aws:sqs:{spec['monitor_region']}:{spec['account_id']}:{queue_name(spec, key, dlq)}"


def report_bucket(spec):
    value = f"{prefix(spec)}-{spec['account_id']}-{spec['monitor_region']}-reports"
    if len(value) > 63:
        raise ValueError("Report bucket name exceeds S3 limit")
    return value


def audit_log_group(spec):
    return f"{log_prefix(spec)}/audit"


def foundation(spec, config):
    t = template(spec, spec["monitor_region"], "Durable incident queues, ledger and private evidence")
    r = t["Resources"]
    r["EvidenceKey"] = resource(
        "KMS::Key",
        {
            "Description": f"{prefix(spec)} incident table and evidence encryption",
            "EnableKeyRotation": True,
            "KeyPolicy": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "AccountIAMAdministration",
                        "Effect": "Allow",
                        "Principal": {"AWS": f"arn:aws:iam::{spec['account_id']}:root"},
                        "Action": "kms:*",
                        "Resource": "*",
                    },
                ],
            },
            "Tags": tagged(spec),
        },
        retain=True,
    )
    r["Evidence"] = resource(
        "S3::Bucket",
        {
            "BucketName": report_bucket(spec),
            "Tags": tagged(spec),
            "VersioningConfiguration": {"Status": "Enabled"},
            "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
            "PublicAccessBlockConfiguration": {
                key: True
                for key in (
                    "BlockPublicAcls",
                    "IgnorePublicAcls",
                    "BlockPublicPolicy",
                    "RestrictPublicBuckets",
                )
            },
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": [
                    {
                        "ServerSideEncryptionByDefault": {
                            "SSEAlgorithm": "aws:kms",
                            "KMSMasterKeyID": att("EvidenceKey"),
                        }
                    }
                ]
            },
            "LifecycleConfiguration": {
                "Rules": [
                    {
                        "Id": "customer-retention",
                        "Status": "Enabled",
                        "ExpirationInDays": config["retention_days"],
                        "NoncurrentVersionExpiration": {"NoncurrentDays": config["retention_days"]},
                        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
                    },
                ]
            },
        },
        retain=True,
    )
    r["EvidencePolicy"] = resource(
        "S3::BucketPolicy",
        {
            "Bucket": ref("Evidence"),
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Deny",
                        "Principal": "*",
                        "Action": "s3:*",
                        "Resource": [att("Evidence"), {"Fn::Sub": "${Evidence.Arn}/*"}],
                        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                    },
                ],
            },
        },
    )
    r["Incidents"] = resource(
        "DynamoDB::Table",
        {
            "TableName": name(spec, "incidents"),
            "BillingMode": "PAY_PER_REQUEST",
            "AttributeDefinitions": [
                {"AttributeName": key, "AttributeType": "S"}
                for key in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK", "GSI3PK", "GSI3SK")
            ],
            "KeySchema": [
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": index,
                    "KeySchema": [
                        {"AttributeName": f"GSI{number}PK", "KeyType": "HASH"},
                        {"AttributeName": f"GSI{number}SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
                for number, index in ((1, "PendingIntents"), (2, "ActiveLeases"), (3, "DueIncidents"))
            ],
            "StreamSpecification": {"StreamViewType": "KEYS_ONLY"},
            "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
            "DeletionProtectionEnabled": True,
            "SSESpecification": {"SSEEnabled": True, "SSEType": "KMS", "KMSMasterKeyId": att("EvidenceKey")},
            "TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True},
            "Tags": tagged(spec),
        },
        retain=True,
    )
    for key in QUEUES:
        r[key + "Dead"] = resource(
            "SQS::Queue",
            {
                "QueueName": queue_name(spec, key, True),
                "MessageRetentionPeriod": 1209600,
                "SqsManagedSseEnabled": True,
                "Tags": tagged(spec),
            },
            retain=True,
        )
        r[key + "Queue"] = resource(
            "SQS::Queue",
            {
                "QueueName": queue_name(spec, key),
                "VisibilityTimeout": 2880 if key == "Work" else 360,
                "MessageRetentionPeriod": 1209600,
                "SqsManagedSseEnabled": True,
                "RedrivePolicy": {"deadLetterTargetArn": att(key + "Dead"), "maxReceiveCount": 5},
                "Tags": tagged(spec),
            },
            retain=True,
        )
        t["Outputs"][key + "QueueArn"] = {"Value": att(key + "Queue")}
        t["Outputs"][key + "QueueUrl"] = {"Value": ref(key + "Queue")}
    ingress_topics = (
        [topic_arn(spec, "alarms"), topic_arn(spec, "canary")]
        if "observability" in spec
        else topic_arn(spec, "alarms")
    )
    r["IngressPolicy"] = resource(
        "SQS::QueuePolicy",
        {
            "Queues": [ref("IngressQueue")],
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        **statement(
                            "sqs:SendMessage", att("IngressQueue"), Principal={"Service": "sns.amazonaws.com"}
                        ),
                        "Condition": {
                            "ArnEquals": {"aws:SourceArn": ingress_topics},
                            "StringEquals": {"aws:SourceAccount": spec["account_id"]},
                        },
                    },
                ],
            },
        },
    )
    # Start durable capture before removing the old direct worker. The ingress
    # mapping is added only at promotion; queued records bridge the cutover.
    r["IngressSubscription"] = resource(
        "SNS::Subscription",
        {
            "TopicArn": topic_arn(spec, "alarms"),
            "Protocol": "sqs",
            "Endpoint": att("IngressQueue"),
            "RawMessageDelivery": False,
            "RedrivePolicy": {"deadLetterTargetArn": att("DeliveryDead")},
        },
        depends=["IngressPolicy", "DeliveryDeadPolicy"],
    )
    r["DeliveryDead"] = resource(
        "SQS::Queue",
        {
            "QueueName": name(spec, "delivery-dead"),
            "MessageRetentionPeriod": 1209600,
            "SqsManagedSseEnabled": True,
            "Tags": tagged(spec),
        },
        retain=True,
    )
    r["DeliveryDeadPolicy"] = resource(
        "SQS::QueuePolicy",
        {
            "Queues": [ref("DeliveryDead")],
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        **statement(
                            "sqs:SendMessage", att("DeliveryDead"), Principal={"Service": "sns.amazonaws.com"}
                        ),
                        "Condition": {
                            "ArnEquals": {"aws:SourceArn": ingress_topics},
                            "StringEquals": {"aws:SourceAccount": spec["account_id"]},
                        },
                    },
                    {
                        **statement(
                            "sqs:SendMessage",
                            att("DeliveryDead"),
                            Principal={"Service": "events.amazonaws.com"},
                        ),
                        "Condition": {
                            "ArnEquals": {
                                "aws:SourceArn": [
                                    f"arn:aws:events:{spec['monitor_region']}:{spec['account_id']}:rule/{name(spec, key)}"
                                    for key in (*event_rules(spec), "incident-sweep")
                                ]
                            },
                            "StringEquals": {"aws:SourceAccount": spec["account_id"]},
                        },
                    },
                ],
            },
        },
    )
    r["DeliveryDeadAlarm"] = resource(
        "CloudWatch::Alarm",
        {
            "AlarmName": name(spec, "delivery-dead"),
            "Namespace": "AWS/SQS",
            "MetricName": "ApproximateNumberOfMessagesVisible",
            "Dimensions": [{"Name": "QueueName", "Value": name(spec, "delivery-dead")}],
            "Statistic": "Maximum",
            "Period": 60,
            "EvaluationPeriods": 1,
            "Threshold": 0,
            "ComparisonOperator": "GreaterThanThreshold",
            "TreatMissingData": "notBreaching",
            "AlarmActions": [ref("FallbackTopic")],
            "Tags": tagged(spec),
        },
    )
    for key in QUEUES:
        r[key + "DeadAlarm"] = resource(
            "CloudWatch::Alarm",
            {
                "AlarmName": name(spec, key.lower() + "-dead"),
                "Namespace": "AWS/SQS",
                "MetricName": "ApproximateNumberOfMessagesVisible",
                "Dimensions": [{"Name": "QueueName", "Value": queue_name(spec, key, True)}],
                "Statistic": "Maximum",
                "Period": 60,
                "EvaluationPeriods": 1,
                "Threshold": 0,
                "ComparisonOperator": "GreaterThanThreshold",
                "TreatMissingData": "notBreaching",
                "AlarmActions": [ref("FallbackTopic")],
                "Tags": tagged(spec),
            },
        )
    r["FallbackTopic"] = resource(
        "SNS::Topic",
        {
            "TopicName": name(spec, "incident-fallback"),
            "Tags": tagged(spec),
        },
        retain=True,
    )
    # The first recipient keeps the original logical ID, so a single address renders as before.
    for index, address in enumerate(fallback_recipients(spec, config)):
        r["FallbackRecipient" + (digest(address)[:16] if index else "")] = resource(
            "SNS::Subscription",
            {
                "TopicArn": ref("FallbackTopic"),
                "Protocol": "email",
                "Endpoint": address,
            },
        )
    r["FallbackPolicy"] = resource(
        "SNS::TopicPolicy",
        {
            "Topics": [ref("FallbackTopic")],
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        **statement(
                            "sns:Publish",
                            ref("FallbackTopic"),
                            Principal={"Service": "cloudwatch.amazonaws.com"},
                        ),
                        "Condition": {
                            "StringEquals": {"aws:SourceAccount": spec["account_id"]},
                            "ArnLike": {
                                "aws:SourceArn": f"arn:aws:cloudwatch:{spec['monitor_region']}:{spec['account_id']}:alarm:{prefix(spec)}-*-dead"
                            },
                        },
                    },
                ],
            },
        },
    )
    r["StreamDead"] = resource(
        "SQS::Queue",
        {
            "QueueName": name(spec, "stream-dead"),
            "MessageRetentionPeriod": 1209600,
            "SqsManagedSseEnabled": True,
            "Tags": tagged(spec),
        },
        retain=True,
    )
    r["AuditLogGroup"] = resource(  # who read which incident and who asked what, written by the team UI
        "Logs::LogGroup",
        {
            "LogGroupName": audit_log_group(spec),
            "RetentionInDays": spec["log_retention_days"],
            "Tags": tagged(spec),
        },
    )
    r["StreamDeadAlarm"] = resource(
        "CloudWatch::Alarm",
        {
            "AlarmName": name(spec, "stream-dead"),
            "Namespace": "AWS/SQS",
            "MetricName": "ApproximateNumberOfMessagesVisible",
            "Dimensions": [{"Name": "QueueName", "Value": name(spec, "stream-dead")}],
            "Statistic": "Maximum",
            "Period": 60,
            "EvaluationPeriods": 1,
            "Threshold": 0,
            "ComparisonOperator": "GreaterThanThreshold",
            "TreatMissingData": "notBreaching",
            "AlarmActions": [ref("FallbackTopic")],
        },
    )
    t["Outputs"].update(
        {
            "TableArn": {"Value": att("Incidents")},
            "TableName": {"Value": ref("Incidents")},
            "StreamArn": {"Value": att("Incidents", "StreamArn")},
            "EvidenceBucket": {"Value": ref("Evidence")},
            "EvidenceKeyArn": {"Value": att("EvidenceKey")},
            "FallbackTopicArn": {"Value": ref("FallbackTopic")},
            "StreamDeadArn": {"Value": att("StreamDead")},
            "DeliveryDeadArn": {"Value": att("DeliveryDead")},
            "AuditLogGroup": {"Value": ref("AuditLogGroup")},
        }
    )
    return t


def runtime(spec, config, artifacts, foundation_outputs, *, owned_bindings):
    t = template(spec, spec["monitor_region"], "Create-only incident runtime candidate")
    table_arn = foundation_outputs["TableArn"]
    bucket = foundation_outputs["EvidenceBucket"]
    key_arn = foundation_outputs["EvidenceKeyArn"]
    queue_arns = {key: foundation_outputs[key + "QueueArn"] for key in QUEUES}

    def table(prefixes, *, check=False, indexes=False):
        """Read and write the incident table, but only items whose partition key starts with one of `prefixes`.
        Index queries carry no key condition (it would apply to the index's own keys), so only the function that
        sweeps the indexes gets them."""
        actions = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:PutItem"]
        if check:  # TransactWriteItems asks for each operation's own action, ConditionCheck included
            actions.append("dynamodb:ConditionCheckItem")
        keys = {
            "ForAllValues:StringLike": {"dynamodb:LeadingKeys": [prefix + "#*" for prefix in prefixes]},
            "Null": {"dynamodb:LeadingKeys": "false"},
        }
        return [statement(actions, table_arn, Condition=keys)] + (
            [statement("dynamodb:Query", table_arn + "/index/*")] if indexes else []
        )

    def sqs_receive(key):
        return statement(
            ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], queue_arns[key]
        )

    common = {
        "MONITOR_REGION": spec["monitor_region"],
        "EXPECTED_ACCOUNT_ID": spec["account_id"],
        "INCIDENT_TABLE": foundation_outputs["TableName"],
        "INCIDENT_RETENTION_DAYS": str(config["retention_days"]),
        "ALLOWED_INSTANCE_IDS": ",".join(targets(spec)),
    }
    if "observability" in spec:
        common.update(
            OBS_NAMESPACE=f"{spec['project']}/{spec['environment']}/Pipeline",
            CANARY_TOPIC_ARN=topic_arn(spec, "canary"),
            TRACK_ALARM_RECOVERY="true",
        )
    definitions = {
        "incident_ingress": (
            "Ingress",
            {
                **common,
                "ALARMS_TOPIC_ARN": topic_arn(spec, "alarms"),
                "ALARM_NAME_PREFIX": prefix(spec) + "-",
                **(
                    {"INCIDENT_COOLDOWN_MINUTES": str(config["incident_cooldown_minutes"])}
                    if "incident_cooldown_minutes" in config
                    else {}
                ),
                **(
                    {"ECS_TARGETS": json.dumps(ecs_ingress(spec), sort_keys=True, separators=(",", ":"))}
                    if ecs_services(spec)
                    else {}
                ),
            },
            [*table(["INCIDENT", "EVENT", "ALARM", "COOLDOWN"], check=True), sqs_receive("Ingress")],
            60,
        ),
        "incident_dispatch": (
            "Dispatch",
            {
                **common,
                **{
                    key.upper() + "_QUEUE_URL": foundation_outputs[key + "QueueUrl"]
                    for key in ("Work", "Initial", "Report")
                },
            },
            [
                *table(["INCIDENT"]),
                statement(
                    [
                        "dynamodb:DescribeStream",
                        "dynamodb:GetRecords",
                        "dynamodb:GetShardIterator",
                        "dynamodb:ListStreams",
                    ],
                    foundation_outputs["StreamArn"],
                ),
                statement(
                    "sqs:SendMessage",
                    [queue_arns[key] for key in ("Work", "Initial", "Report")]
                    + [foundation_outputs["StreamDeadArn"]],
                ),
            ],
            60,
        ),
        "incident_investigate": (
            "Investigate",
            {
                **common,
                "REPORT_BUCKET": bucket,
                "REPORT_KMS_KEY_ARN": key_arn,
                "BEDROCK_REGION": spec["bedrock_region"],
            },
            [
                *table(["INCIDENT", "EVENT"]),
                sqs_receive("Work"),
                statement("s3:PutObject", f"arn:aws:s3:::{bucket}/incidents/*"),
                statement(["kms:Encrypt", "kms:GenerateDataKey"], key_arn),
            ],
            480,
        ),
        "incident_initial": (
            "Initial",
            {
                **common,
                "REPORTS_TOPIC_ARN": topic_arn(spec, "reports"),
                "STATUS_BASE_URL": config["status_base_url"],
            },
            [
                *table(["INCIDENT"]),
                sqs_receive("Initial"),
                statement("sns:Publish", topic_arn(spec, "reports")),
            ],
            60,
        ),
        "incident_report": (
            "Report",
            {
                **common,
                "REPORTS_TOPIC_ARN": topic_arn(spec, "reports"),
                "STATUS_BASE_URL": config["status_base_url"],
            },
            [
                *table(["INCIDENT"]),
                sqs_receive("Report"),
                statement("sns:Publish", topic_arn(spec, "reports")),
            ],
            60,
        ),
        "incident_reconcile": (
            "Reconcile",
            {
                **common,
                **{
                    key.upper() + "_QUEUE_URL": foundation_outputs[key + "QueueUrl"]
                    for key in ("Work", "Initial", "Report")
                },
            },
            [
                *table(["INCIDENT", "SWEEP"], indexes=True),
                statement("sqs:SendMessage", [queue_arns[key] for key in ("Work", "Initial", "Report")]),
                statement("sqs:SendMessage", foundation_outputs["StreamDeadArn"]),
            ],
            60,
        ),
    }
    if owned_bindings is not None:
        from infra.stacks.owned_runtime import caller_environment, caller_permissions

        logical, previous_env, permissions, timeout = definitions["incident_investigate"]
        model_env = {key: value for key, value in previous_env.items() if not key.startswith("BEDROCK_")}
        model_env.update(caller_environment(spec, config, owned_bindings))
        model_env["ALLOW_RUNTIME_CANARY"] = "true" if spec["environment"] == "staging" else "false"
        definitions["incident_investigate"] = (
            logical,
            model_env,
            permissions + caller_permissions(spec, config, owned_bindings),
            timeout,
        )
    for function in FUNCTIONS:
        logical, env, permissions, timeout = definitions[function]
        add_function(
            t, spec, logical, function, artifacts[function], env, spec["monitor_region"], permissions, timeout
        )
    t["Resources"]["Initial"]["Properties"]["ReservedConcurrentExecutions"] = config[
        "initial_reserved_concurrency"
    ]
    t["Resources"]["ReconcileFailure"] = resource(
        "Lambda::EventInvokeConfig",
        {
            "FunctionName": ref("Reconcile"),
            "Qualifier": att("ReconcileVersion", "Version"),
            "MaximumRetryAttempts": 2,
            "MaximumEventAgeInSeconds": 600,
            "DestinationConfig": {"OnFailure": {"Destination": foundation_outputs["StreamDeadArn"]}},
        },
    )
    return t


def active_routing(
    spec,
    foundation_outputs,
    versions,
    investigation_paused=False,
    *,
    config=None,
    owned_bindings=None,
):
    t = service_routing(spec)
    r = t["Resources"]
    r["UiRole"] = role(spec, spec["ui_principal_arn"], [], service=False)
    t["Outputs"]["UiRoleArn"] = {"Value": att("UiRole")}
    if owned_bindings is not None:
        from infra.stacks.owned_runtime import caller_environment, caller_permissions

        r["UiRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"] = caller_permissions(
            spec, config, owned_bindings
        )
        t["Outputs"]["RuntimeConnection"] = {
            "Value": json.dumps(caller_environment(spec, config, owned_bindings))
        }
    for logical in event_rules(spec).values():
        r[logical]["Properties"]["Targets"][0].update(
            {
                "DeadLetterConfig": {"Arn": foundation_outputs["DeliveryDeadArn"]},
                "RetryPolicy": {"MaximumEventAgeInSeconds": 600, "MaximumRetryAttempts": 5},
            }
        )
    r["UiRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"] += [
        statement(
            "dynamodb:GetItem",
            foundation_outputs["TableArn"],
            Condition={
                "ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["INCIDENT#*"]},
                "Null": {"dynamodb:LeadingKeys": "false"},
            },
        ),
        statement(
            ["logs:CreateLogStream", "logs:PutLogEvents"],
            f"arn:aws:logs:{spec['monitor_region']}:{spec['account_id']}:log-group:{foundation_outputs['AuditLogGroup']}:*",
        ),
        statement("s3:GetObjectVersion", f"arn:aws:s3:::{foundation_outputs['EvidenceBucket']}/incidents/*"),
        statement("kms:Decrypt", foundation_outputs["EvidenceKeyArn"]),
    ]
    sources = {
        "Ingress": ("IngressQueueArn", "Ingress", 2),
        "Work": ("WorkQueueArn", "Investigate", 2),
        "Initial": ("InitialQueueArn", "Initial", 2),
        "Report": ("ReportQueueArn", "Report", 2),
    }
    for key, (queue, function, batch) in sources.items():
        r[key + "Mapping"] = resource(
            "Lambda::EventSourceMapping",
            {
                "EventSourceArn": foundation_outputs[queue],
                "FunctionName": versions[function + "VersionArn"],
                "BatchSize": batch,
                "FunctionResponseTypes": ["ReportBatchItemFailures"],
                "Enabled": not spec["maintenance_mode"],
            },
        )
        if key == "Work":
            r[key + "Mapping"]["Properties"]["BatchSize"] = 1
            r[key + "Mapping"]["Properties"]["ScalingConfig"] = {"MaximumConcurrency": 2}
            r[key + "Mapping"]["Properties"]["Enabled"] = (
                not spec["maintenance_mode"] and not investigation_paused
            )
    r["StreamMapping"] = resource(
        "Lambda::EventSourceMapping",
        {
            "EventSourceArn": foundation_outputs["StreamArn"],
            "FunctionName": versions["DispatchVersionArn"],
            "StartingPosition": "TRIM_HORIZON",
            "BatchSize": 10,
            "FunctionResponseTypes": ["ReportBatchItemFailures"],
            "MaximumRetryAttempts": 5,
            "DestinationConfig": {"OnFailure": {"Destination": foundation_outputs["StreamDeadArn"]}},
            "Enabled": not spec["maintenance_mode"],
        },
    )
    r["SweepRule"] = resource(
        "Events::Rule",
        {
            "Name": name(spec, "incident-sweep"),
            "ScheduleExpression": "rate(1 minute)",
            "State": "DISABLED" if spec["maintenance_mode"] else "ENABLED",
            "Targets": [
                {
                    "Id": "sweep-" + kind,
                    "Input": json.dumps({"sweep": kind}),
                    "Arn": versions["ReconcileVersionArn"],
                    "DeadLetterConfig": {"Arn": foundation_outputs["DeliveryDeadArn"]},
                    "RetryPolicy": {"MaximumEventAgeInSeconds": 600, "MaximumRetryAttempts": 2},
                }
                for kind in ("pending", "overdue", "expired", "notifications")
            ],
        },
    )
    r["SweepPermission"] = resource(
        "Lambda::Permission",
        {
            "Action": "lambda:InvokeFunction",
            "FunctionName": versions["ReconcileVersionArn"],
            "Principal": "events.amazonaws.com",
            "SourceAccount": spec["account_id"],
            "SourceArn": f"arn:aws:events:{spec['monitor_region']}:{spec['account_id']}:rule/{name(spec, 'incident-sweep')}",
        },
    )
    return t
