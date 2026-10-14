"""observation resources: independent delivery witness, observers and monitored escalation."""

import json

from infra.spec import (
    alarm_descriptors,
    digest,
    fallback_recipients,
    log_groups,
    log_prefix,
    name,
    recipients,
    topic_arn,
)
from infra.stacks import durable_templates, owned_runtime
from infra.stacks.templates import add_function, att, ref, resource, statement, tagged, template

FUNCTIONS = {
    "observation_probe": "Observer",
    "observation_canary": "Canary",
    "observation_receipt": "Receipt",
}


def queue_arn(spec, suffix):
    return f"arn:aws:sqs:{spec['monitor_region']}:{spec['account_id']}:{name(spec, suffix)}"


def foundation(spec, outputs, config):
    t = template(spec, spec["monitor_region"], "Independent canary capture, receipt queue and escalation")
    r = t["Resources"]
    for logical, suffix in (("CanaryTopic", "canary"), ("Escalation", "observation-fallback")):
        r[logical] = resource(
            "SNS::Topic", {"TopicName": name(spec, suffix), "Tags": tagged(spec)}, retain=True
        )
    for index, address in enumerate(fallback_recipients(spec, config)):
        r["EscalationRecipient" + (digest(address)[:16] if index else "")] = resource(
            "SNS::Subscription",
            {"TopicArn": ref("Escalation"), "Protocol": "email", "Endpoint": address},
        )
    r["EscalationPolicy"] = resource(
        "SNS::TopicPolicy",
        {
            "Topics": [ref("Escalation")],
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    statement(
                        "sns:Publish",
                        ref("Escalation"),
                        Principal={"Service": "cloudwatch.amazonaws.com"},
                        Condition={
                            "StringEquals": {"aws:SourceAccount": spec["account_id"]},
                            "ArnLike": {
                                "aws:SourceArn": f"arn:aws:cloudwatch:{spec['monitor_region']}:{spec['account_id']}:alarm:{name(spec, 'obs-')}*"
                            },
                        },
                    )
                ],
            },
        },
    )
    for logical, suffix in (("Dead", "observation-dead"), ("Receipts", "observation-receipts")):
        properties = {
            "QueueName": name(spec, suffix),
            "SqsManagedSseEnabled": True,
            "MessageRetentionPeriod": 1209600,
            "Tags": tagged(spec),
        }
        if logical == "Receipts":
            properties.update(
                VisibilityTimeout=180,
                RedrivePolicy={"deadLetterTargetArn": att("Dead"), "maxReceiveCount": 5},
            )
        r[logical] = resource("SQS::Queue", properties, retain=True)
    r["ReceiptPolicy"] = resource(
        "SQS::QueuePolicy",
        {
            "Queues": [ref("Receipts")],
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    statement(
                        "sqs:SendMessage",
                        att("Receipts"),
                        Principal={"Service": "sns.amazonaws.com"},
                        Condition={
                            "ArnEquals": {"aws:SourceArn": topic_arn(spec, "reports")},
                            "StringEquals": {"aws:SourceAccount": spec["account_id"]},
                        },
                    )
                ],
            },
        },
    )
    r["DeadPolicy"] = resource(
        "SQS::QueuePolicy",
        {
            "Queues": [ref("Dead")],
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    statement(
                        "sqs:SendMessage",
                        att("Dead"),
                        Principal={"Service": "sns.amazonaws.com"},
                        Condition={
                            "ArnEquals": {"aws:SourceArn": topic_arn(spec, "reports")},
                            "StringEquals": {"aws:SourceAccount": spec["account_id"]},
                        },
                    ),
                    statement(
                        "sqs:SendMessage",
                        att("Dead"),
                        Principal={"Service": "events.amazonaws.com"},
                        Condition={
                            "ArnEquals": {
                                "aws:SourceArn": [
                                    f"arn:aws:events:{spec['monitor_region']}:{spec['account_id']}:rule/{name(spec, 'obs-' + job)}"
                                    for job in (
                                        "probe",
                                        "canary",
                                        *(
                                            "health-" + service["id"]
                                            for service in spec["observability"]["services"]
                                        ),
                                    )
                                ]
                            },
                            "StringEquals": {"aws:SourceAccount": spec["account_id"]},
                        },
                    ),
                ],
            },
        },
    )
    r["ReceiptSubscription"] = resource(
        "SNS::Subscription",
        {
            "TopicArn": topic_arn(spec, "reports"),
            "Protocol": "sqs",
            "Endpoint": att("Receipts"),
            "RawMessageDelivery": False,
            "FilterPolicy": {"tracaus_canary": ["true"]},
            "RedrivePolicy": {"deadLetterTargetArn": att("Dead")},
        },
        depends=["ReceiptPolicy", "DeadPolicy"],
    )
    r["CanaryIngress"] = resource(
        "SNS::Subscription",
        {
            "TopicArn": ref("CanaryTopic"),
            "Protocol": "sqs",
            "Endpoint": outputs["IngressQueueArn"],
            "RawMessageDelivery": False,
            "RedrivePolicy": {"deadLetterTargetArn": outputs["DeliveryDeadArn"]},
        },
    )
    t["Outputs"] = {
        "ReceiptQueueArn": {"Value": att("Receipts")},
        "ReceiptQueueUrl": {"Value": ref("Receipts")},
        "ObservationDeadArn": {"Value": att("Dead")},
        "EscalationTopicArn": {"Value": ref("Escalation")},
    }
    return t


def runtime(spec, outputs, artifacts, durable_config):
    t = template(spec, spec["monitor_region"], "Create-only external observer and independent canary runtime")
    table = outputs["TableArn"]
    config = spec["observability"]
    common = {
        "MONITOR_REGION": spec["monitor_region"],
        "EXPECTED_ACCOUNT_ID": spec["account_id"],
        "INCIDENT_TABLE": outputs["TableName"],
        "OBS_SETTINGS": json.dumps(config, separators=(",", ":")),
        "OBS_NAMESPACE": f"{spec['project']}/{spec['environment']}/Pipeline",
        "HEALTH_NAMESPACE": f"{spec['project']}/{spec['environment']}/Health",
        "LOG_GROUP_PREFIX": log_prefix(spec),
        "CANARY_TOPIC_ARN": topic_arn(spec, "canary"),
        "ALARMS_TOPIC_ARN": topic_arn(spec, "alarms"),
        "ALARM_NAME_PREFIX": name(spec, ""),
        "REPORTS_TOPIC_ARN": topic_arn(spec, "reports"),
        "PRIMARY_EMAIL": ",".join(recipients(spec)),
        "FALLBACK_TOPIC_ARN": topic_arn(spec, "observation-fallback"),
        "FALLBACK_EMAIL": ",".join(fallback_recipients(spec, durable_config)),
        "MAINTENANCE_MODE": str(spec["maintenance_mode"]).lower(),
    }
    read = statement(
        "dynamodb:GetItem",
        table,
        Condition={
            "ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["CANARY#*", "INCIDENT#*", "OBS#EMAIL"]}
        },
    )
    write = statement(
        ["dynamodb:PutItem", "dynamodb:UpdateItem"],
        table,
        Condition={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["CANARY#*"]}},
    )
    for function, logical in FUNCTIONS.items():
        scoped_read = {
            **read,
            "Condition": {
                "ForAllValues:StringLike": {
                    "dynamodb:LeadingKeys": ["CANARY#*"]
                    if logical == "Canary"
                    else ["INCIDENT#*"]
                    if logical == "Receipt"
                    else ["CANARY#*", "INCIDENT#*", "OBS#EMAIL"]
                }
            },
        }
        permissions = [scoped_read]
        if logical == "Observer":
            permissions += [
                statement("dynamodb:Query", table + "/index/PendingIntents"),
                statement("cloudwatch:GetMetricStatistics", "*"),
                statement(
                    "cloudwatch:PutMetricData",
                    "*",
                    Condition={"StringEquals": {"cloudwatch:namespace": common["HEALTH_NAMESPACE"]}},
                ),
                statement(
                    "logs:GetLogEvents",
                    [
                        f"arn:aws:logs:{spec['monitor_region']}:{spec['account_id']}:log-group:{log_prefix(spec)}/{s['instance_id']}/{s['heartbeat_log_group']}:log-stream:{s['instance_id']}"
                        for s in config["services"]
                    ],
                ),
                statement(
                    "sns:ListSubscriptionsByTopic",
                    [topic_arn(spec, "reports"), topic_arn(spec, "observation-fallback")],
                ),
                statement(
                    "sns:GetSubscriptionAttributes",
                    [topic_arn(spec, "reports") + ":*", topic_arn(spec, "observation-fallback") + ":*"],
                ),
            ]
        elif logical == "Canary":
            permissions += [write, statement("sns:Publish", topic_arn(spec, "canary"))]
        else:
            permissions += [
                write,
                statement(
                    ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
                    queue_arn(spec, "observation-receipts"),
                ),
            ]
        permissions += [statement("sqs:SendMessage", queue_arn(spec, "observation-dead"))]
        if sum(len(k.encode()) + len(v.encode()) for k, v in common.items()) > 4000:
            raise ValueError("Observer environment exceeds Lambda's size limit")
        add_function(
            t,
            spec,
            logical,
            function,
            artifacts[function],
            common,
            spec["monitor_region"],
            permissions,
            180 if logical == "Observer" else 30,
        )
        if logical != "Receipt":
            t["Resources"][logical + "Async"] = resource(
                "Lambda::EventInvokeConfig",
                {
                    "FunctionName": ref(logical),
                    "Qualifier": att(logical + "Version", "Version"),
                    "MaximumRetryAttempts": 0,
                    "MaximumEventAgeInSeconds": 600,
                    "DestinationConfig": {"OnFailure": {"Destination": queue_arn(spec, "observation-dead")}},
                },
            )
    return t


def active(spec, outputs, versions, pipeline_versions, *, runtime_target="standalone", agentcore=None):
    t = template(
        spec, spec["monitor_region"], "Independent detection schedules, alarms and pipeline dashboard"
    )
    r = t["Resources"]
    config = spec["observability"]
    enabled = config["enabled"]
    if runtime_target not in {"standalone", "agentcore"}:
        raise ValueError("Unknown monitored runtime target")
    if runtime_target == "agentcore":
        if not agentcore:
            raise ValueError("AgentCore observation requires explicit runtime and endpoint bindings")
        from tracaus.agentcore import validate_target

        validate_target(
            agentcore["RuntimeArn"], agentcore["EndpointName"], spec["bedrock_region"], spec["account_id"]
        )
    topic = topic_arn(spec, "observation-fallback")

    def alarm(logical, namespace, metric, dimensions, threshold=0, missing="notBreaching", periods=1):
        r[logical] = resource(
            "CloudWatch::Alarm",
            {
                "AlarmName": name(spec, "obs-" + logical.lower()),
                "AlarmDescription": "Owner: deployment-oncall; docs/OPERATE.md",
                "Namespace": namespace,
                "MetricName": metric,
                "Dimensions": [{"Name": k, "Value": v} for k, v in dimensions.items()],
                "Statistic": "Maximum" if metric.endswith("AgeOfOldestMessage") else "Sum",
                "Period": config["interval_minutes"] * 60,
                "EvaluationPeriods": periods,
                "Threshold": threshold,
                "ComparisonOperator": "GreaterThanThreshold",
                "TreatMissingData": missing,
                "ActionsEnabled": enabled,
                "AlarmActions": [topic],
                "OKActions": [topic],
                "Tags": tagged(spec),
            },
        )

    for logical, job in (("Observer", "probe"), ("Canary", "canary")):
        rule = name(spec, "obs-" + job)
        r[logical + "Schedule"] = resource(
            "Events::Rule",
            {
                "Name": rule,
                "ScheduleExpression": f"rate({config['interval_minutes']} minutes)",
                "State": "ENABLED" if enabled else "DISABLED",
                "Targets": [
                    {
                        "Id": job,
                        **({"Input": json.dumps({"mode": "delivery"})} if logical == "Observer" else {}),
                        "Arn": versions[logical + "VersionArn"],
                        "DeadLetterConfig": {"Arn": queue_arn(spec, "observation-dead")},
                        "RetryPolicy": {"MaximumRetryAttempts": 1, "MaximumEventAgeInSeconds": 300},
                    }
                ],
            },
        )
        r[logical + "Permission"] = resource(
            "Lambda::Permission",
            {
                "Action": "lambda:InvokeFunction",
                "FunctionName": versions[logical + "VersionArn"],
                "Principal": "events.amazonaws.com",
                "SourceAccount": spec["account_id"],
                "SourceArn": f"arn:aws:events:{spec['monitor_region']}:{spec['account_id']}:rule/{rule}",
            },
        )
    hours = config["canary_interval_minutes"] // 60
    r["CanarySchedule"]["Properties"]["ScheduleExpression"] = (
        "cron(0 0 * * ? *)" if hours == 24 else f"cron(0 0/{hours} * * ? *)"
    )
    for number, service in enumerate(config["services"]):
        logical = "Service" + str(number)
        rule = name(spec, "obs-health-" + service["id"])
        r[logical + "Schedule"] = resource(
            "Events::Rule",
            {
                "Name": rule,
                "ScheduleExpression": f"rate({config['interval_minutes']} minutes)",
                "State": "ENABLED" if enabled and not spec["maintenance_mode"] else "DISABLED",
                "Targets": [
                    {
                        "Id": service["id"],
                        "Arn": versions["ObserverVersionArn"],
                        "Input": json.dumps({"mode": "health", "service_id": service["id"]}),
                        "DeadLetterConfig": {"Arn": queue_arn(spec, "observation-dead")},
                        "RetryPolicy": {"MaximumRetryAttempts": 1, "MaximumEventAgeInSeconds": 300},
                    }
                ],
            },
        )
        r[logical + "Permission"] = resource(
            "Lambda::Permission",
            {
                "Action": "lambda:InvokeFunction",
                "FunctionName": versions["ObserverVersionArn"],
                "Principal": "events.amazonaws.com",
                "SourceAccount": spec["account_id"],
                "SourceArn": f"arn:aws:events:{spec['monitor_region']}:{spec['account_id']}:rule/{rule}",
            },
        )
    r["ReceiptMapping"] = resource(
        "Lambda::EventSourceMapping",
        {
            "EventSourceArn": queue_arn(spec, "observation-receipts"),
            "FunctionName": versions["ReceiptVersionArn"],
            "BatchSize": 2,
            "FunctionResponseTypes": ["ReportBatchItemFailures"],
            "Enabled": enabled,
        },
    )
    alarm(
        "ObserverFailure",
        f"{spec['project']}/{spec['environment']}/Pipeline",
        "Failure",
        {"Component": "observer"},
        missing="breaching",
    )
    alarm(
        "ObserverHeartbeat",
        f"{spec['project']}/{spec['environment']}/Pipeline",
        "Heartbeat",
        {"Component": "observer"},
        threshold=1,
        missing="breaching",
        periods=3,
    )
    r["ObserverHeartbeat"]["Properties"].update(ComparisonOperator="LessThanThreshold")
    alarm(
        "ObservationDead",
        "AWS/SQS",
        "ApproximateNumberOfMessagesVisible",
        {"QueueName": name(spec, "observation-dead")},
    )
    for key in durable_templates.QUEUES:
        alarm(
            key + "Age",
            "AWS/SQS",
            "ApproximateAgeOfOldestMessage",
            {"QueueName": durable_templates.queue_name(spec, key)},
            threshold=300,
        )
        r[key + "Age"]["Properties"]["ActionsEnabled"] = enabled and not spec["maintenance_mode"]
    all_versions = {**pipeline_versions, **versions}
    for logical, arn in all_versions.items():
        function = arn.split(":function:")[-1].rsplit(":", 1)[0]
        alarm(logical + "Errors", "AWS/Lambda", "Errors", {"FunctionName": function})
        alarm(logical + "Throttles", "AWS/Lambda", "Throttles", {"FunctionName": function})
    alarm(
        "DeniedNotifications", "AWS/SNS", "NumberOfNotificationsFailed", {"TopicName": name(spec, "reports")}
    )
    component_metrics = {
        "ingress": ("Accepted", "Duplicate", "Failure", "QueueDelaySeconds"),
        "dispatch": ("Failure",),
        "work": ("Attempt", "Failure", "ReportPersisted"),
        "model": ("ModelCalls", "InputTokens", "OutputTokens", "Deadline", "DailyBudget", "Failure"),
        "tool": ("ToolFailure", "ToolNoData"),
        "initial": ("NotificationAccepted", "Failure"),
        "report": ("NotificationAccepted", "Failure"),
        "reconcile": ("Heartbeat", "SweepPending", "Failure"),
        "observer": ("Heartbeat", "Failure", "OldestOutboxSeconds", "EmailReceiptAgeSeconds"),
        "canary": ("Heartbeat",),
        "receipt": ("RecipientReceived", "Failure"),
    }
    alarm(
        "FallbackDeliveryFailed",
        "AWS/SNS",
        "NumberOfNotificationsFailed",
        {"TopicName": name(spec, "observation-fallback")},
    )
    r["FallbackDeliveryFailed"]["Properties"]["AlarmActions"] = [topic_arn(spec, "reports")]
    r["FallbackDeliveryFailed"]["Properties"]["OKActions"] = [topic_arn(spec, "reports")]
    for logical in ("ObserverFailure", "ObserverHeartbeat"):
        r[logical]["Properties"]["AlarmActions"].append(topic_arn(spec, "reports"))
        r[logical]["Properties"]["OKActions"].append(topic_arn(spec, "reports"))
    widgets = []
    for component in component_metrics:
        widgets.append(
            {
                "type": "metric",
                "width": 8,
                "height": 6,
                "properties": {
                    "region": spec["bedrock_region"]
                    if runtime_target == "agentcore" and component in {"model", "tool"}
                    else spec["monitor_region"],
                    "title": component,
                    "period": 300,
                    "metrics": [
                        [f"{spec['project']}/{spec['environment']}/Pipeline", metric, "Component", component]
                        for metric in component_metrics[component]
                    ],
                    "stat": "Sum",
                },
            }
        )
    for title, metrics in (
        (
            "Queue age and dead letters",
            [
                [
                    "AWS/SQS",
                    "ApproximateAgeOfOldestMessage",
                    "QueueName",
                    durable_templates.queue_name(spec, key),
                ]
                for key in durable_templates.QUEUES
            ]
            + [
                ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", name(spec, "observation-dead")]
            ],
        ),
        (
            "Lambda errors and throttles",
            [
                ["AWS/Lambda", metric, "FunctionName", arn.split(":function:")[-1].rsplit(":", 1)[0]]
                for arn in all_versions.values()
                for metric in ("Errors", "Throttles")
            ],
        ),
        (
            "Service and telemetry health",
            [
                [
                    f"{spec['project']}/{spec['environment']}/Health",
                    d["metric_name"],
                    "Service",
                    d["dimensions"]["Service"],
                    "Route",
                    d["dimensions"]["Route"],
                ]
                for d in alarm_descriptors(spec)
                if d["namespace"].endswith("/Health")
            ],
        ),
    ):
        widgets.append(
            {
                "type": "metric",
                "width": 24,
                "height": 6,
                "properties": {
                    "region": spec["monitor_region"],
                    "title": title,
                    "period": 300,
                    "stat": "Maximum",
                    "metrics": metrics,
                },
            }
        )
    widgets.append(
        {
            "type": "log",
            "width": 24,
            "height": 6,
            "properties": {
                "region": spec["monitor_region"],
                "title": "Incident handoffs (IDs are log fields)",
                "query": " | ".join(
                    [
                        " | ".join(
                            "SOURCE '/aws/lambda/" + arn.split(":function:")[-1].rsplit(":", 1)[0] + "'"
                            for arn in all_versions.values()
                        ),
                        "fields @timestamp, Component, outcome, incident_id, fence",
                        "sort @timestamp desc",
                        "limit 100",
                    ]
                ),
            },
        }
    )
    if runtime_target == "agentcore":
        runtime_id = agentcore["RuntimeArn"].split("/")[-1]
        runtime_name = runtime_id.rsplit("-", 1)[0]
        widgets.append(
            {
                "type": "metric",
                "width": 24,
                "height": 6,
                "properties": {
                    "region": spec["bedrock_region"],
                    "title": "AgentCore release endpoint health",
                    "period": 300,
                    "stat": "Sum",
                    "metrics": [
                        [
                            "AWS/Bedrock-AgentCore",
                            metric,
                            "Operation",
                            "InvokeAgentRuntime",
                            "Name",
                            runtime_name + "::" + agentcore["EndpointName"],
                            "Resource",
                            agentcore["RuntimeArn"],
                            {"stat": "Average" if metric == "Latency" else "Sum"},
                        ]
                        for metric in ("Invocations", "Throttles", "SystemErrors", "UserErrors", "Latency")
                    ],
                },
            }
        )
        widgets.append(
            {
                "type": "log",
                "width": 24,
                "height": 6,
                "properties": {
                    "region": spec["bedrock_region"],
                    "title": "AgentCore application handoffs",
                    "query": "SOURCE '"
                    + owned_runtime.agentcore_log_group(runtime_id, agentcore["EndpointName"])
                    + "' | fields @timestamp, @message | sort @timestamp desc | limit 100",
                },
            }
        )
    r["Dashboard"] = resource(
        "CloudWatch::Dashboard",
        {"DashboardName": name(spec, "operations"), "DashboardBody": json.dumps({"widgets": widgets})},
    )
    t["Outputs"]["DashboardName"] = {"Value": ref("Dashboard")}
    t["Outputs"]["AlarmOwners"] = {"Value": "deployment-oncall; service owners in the coverage manifest"}
    return t


def coverage_manifest(spec, rendered=()):
    from infra.spec import alarm_descriptors, metric_catalog

    descriptors = {d["id"]: d for d in metric_catalog(spec)}
    services = spec["observability"]["services"]
    operational = []
    service_alarms = {d["alarm_name"] for d in alarm_descriptors(spec)}
    seen = set()
    for stage in rendered:
        for resource_value in stage["Resources"].values():
            if resource_value["Type"] != "AWS::CloudWatch::Alarm":
                continue
            p = resource_value["Properties"]
            if p["AlarmName"] in service_alarms or p["AlarmName"] in seen:
                continue
            seen.add(p["AlarmName"])
            operational.append(
                {
                    "alarm": p["AlarmName"],
                    "owner": "deployment-oncall",
                    "descriptor": {
                        k: p[k]
                        for k in (
                            "Namespace",
                            "MetricName",
                            "Dimensions",
                            "Statistic",
                            "Period",
                            "Threshold",
                            "ComparisonOperator",
                            "TreatMissingData",
                        )
                    },
                    "actions": p["AlarmActions"],
                    "enabled": p.get("ActionsEnabled", True),
                }
            )
    return {
        "version": 1,
        "operational_alarm_evidence": operational,
        "spec_hash": digest(spec),
        "inventory_policy": "static; regenerate reviewed release on fleet changes; no implicit autoscaling discovery",
        "alarm_evidence": [
            {
                "alarm": d["alarm_name"],
                "descriptor": descriptors[d["id"]],
                "owner": d.get("owner")
                or next(s["owner"] for s in services if s["instance_id"] == d["instance_id"]),
                "missing_data": d.get(
                    "missing_data", "notBreaching" if d["namespace"].endswith("/Nginx") else "missing"
                ),
                "counting": "access requests"
                if d["id"].endswith("nginx-requests")
                else "diagnostic log events"
                if d["id"].endswith("nginx-diagnostics")
                else "metric samples",
            }
            for d in alarm_descriptors(spec)
        ],
        "log_groups": log_groups(spec),
        "queue_owner": "deployment-oncall; every pipeline and observation queue/DLQ",
        "log_retention_days": spec["log_retention_days"],
    }
