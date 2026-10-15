"""Generate reviewable regional CloudFormation stacks from one inventory.

Release stacks are create-only. Stable routing is a separate, gated change.
"""

import base64
import hashlib
import json

from infra.spec import (
    alarm_descriptors,
    boundary_arn,
    digest,
    ecs_services,
    ecs_targets,
    event_rules,
    existing_log_groups,
    log_group_arn,
    log_groups,
    log_prefix,
    name,
    prefix,
    readable_log_groups,
    recipients,
    tags,
    targets,
    topic_arn,
)


def ref(key):
    return {"Ref": key}


def att(key, attribute="Arn"):
    return {"Fn::GetAtt": [key, attribute]}


def tagged(spec, release=False):
    return [{"Key": key, "Value": value} for key, value in tags(spec, release).items()]


def resource(kind, properties, retain=False, depends=None):
    value = {"Type": f"AWS::{kind}", "Properties": properties}
    if retain:
        value.update(DeletionPolicy="Retain", UpdateReplacePolicy="Retain")
    if depends:
        value["DependsOn"] = depends
    return value


def template(spec, region, description):
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": description,
        "Metadata": {
            "Tracaus": {**tags(spec), "ExpectedAccount": spec["account_id"], "ExpectedRegion": region}
        },
        "Resources": {},
        "Outputs": {},
    }


def statement(actions, resources, **extra):
    return {"Effect": "Allow", "Action": actions, "Resource": resources, **extra}


def role(spec, principal, statements, *, service=True, conditions=None, release=False):
    trust = {
        "Effect": "Allow",
        "Principal": {"Service" if service else "AWS": principal},
        "Action": "sts:AssumeRole",
    }
    if conditions:
        trust["Condition"] = conditions
    return resource(
        "IAM::Role",
        {
            "Path": f"/{spec['project']}/{spec['environment']}/",
            "PermissionsBoundary": boundary_arn(spec),
            "Tags": tagged(spec, release),
            "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [trust]},
            "Policies": [
                {
                    "PolicyName": "runtime",
                    "PolicyDocument": {"Version": "2012-10-17", "Statement": statements},
                }
            ],
        },
        retain=release,
    )


def bucket_name(spec, purpose):
    # Global S3 namespace; account and region are explicit. Two buckets even in a same-region deployment.
    region = spec["bedrock_region"] if purpose == "tools" else spec["monitor_region"]
    value = f"{prefix(spec)}-{spec['account_id']}-{region}-{purpose}"
    if len(value) > 63:
        raise ValueError("Artifact bucket name exceeds 63 characters; shorten project name")
    return value


def foundation(spec, purpose):
    region = spec["bedrock_region"] if purpose == "tools" else spec["monitor_region"]
    t = template(spec, region, f"Tracaus {purpose} foundation; retained private artifacts and telemetry")
    r = t["Resources"]
    r["Artifacts"] = resource(
        "S3::Bucket",
        {
            "BucketName": bucket_name(spec, purpose),
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
                    {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                ]
            },
        },
        retain=True,
    )
    r["ArtifactPolicy"] = resource(
        "S3::BucketPolicy",
        {
            "Bucket": ref("Artifacts"),
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Deny",
                        "Principal": "*",
                        "Action": "s3:*",
                        "Resource": [att("Artifacts"), {"Fn::Sub": "${Artifacts.Arn}/*"}],
                        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                    }
                ],
            },
        },
    )
    t["Outputs"]["ArtifactBucket"] = {"Value": ref("Artifacts")}
    if purpose == "tools":
        r["CursorSecret"] = resource(
            "SecretsManager::Secret",
            {
                "Name": f"{prefix(spec)}/log-cursor",
                "Description": "Pin a VersionId per release; never put the value in a plan",
                "GenerateSecretString": {"PasswordLength": 48, "ExcludePunctuation": True},
                "Tags": tagged(spec),
            },
            retain=True,
        )
        t["Outputs"]["CursorSecretArn"] = {"Value": ref("CursorSecret")}
        r["CiValidationRole"] = role(
            spec,
            spec["ci_principal_arn"],
            [
                statement(
                    [
                        "cloudformation:DescribeStacks",
                        "cloudformation:DescribeStackEvents",
                        "cloudformation:DescribeChangeSet",
                        "cloudformation:ListStackResources",
                    ],
                    f"arn:aws:cloudformation:*:{spec['account_id']}:stack/{prefix(spec)}-*/*",
                ),
                statement(["s3:GetObjectVersion"], f"arn:aws:s3:::{bucket_name(spec, 'tools')}/*"),
            ],
            service=False,
        )
    else:
        # Identities are provisioned now; their durable runtime bindings belong
        # to the separately rendered pipeline. No queue or asynchronous pipeline is implied here.
        r["IngestionRole"] = role(
            spec,
            "lambda.amazonaws.com",
            [
                statement(
                    "sqs:SendMessage", f"arn:aws:sqs:{region}:{spec['account_id']}:{name(spec, 'incidents')}"
                )
            ],
        )
        r["NotificationRole"] = role(
            spec, "lambda.amazonaws.com", [statement("sns:Publish", topic_arn(spec, "reports"))]
        )
        for key, suffix in (("AlarmsTopic", "alarms"), ("ReportsTopic", "reports")):
            r[key] = resource(
                "SNS::Topic", {"TopicName": name(spec, suffix), "Tags": tagged(spec)}, retain=True
            )
            t["Outputs"][key + "Arn"] = {"Value": ref(key)}
        rule_arns = [
            f"arn:aws:events:{region}:{spec['account_id']}:rule/{name(spec, key)}"
            for key in event_rules(spec)
        ]
        r["TopicPolicies"] = resource(
            "SNS::TopicPolicy",
            {
                "Topics": [ref("AlarmsTopic"), ref("ReportsTopic")],
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        statement(
                            "sns:Publish",
                            ref("AlarmsTopic"),
                            Principal={"Service": "events.amazonaws.com"},
                            Condition={
                                "StringEquals": {"aws:SourceAccount": spec["account_id"]},
                                "ArnEquals": {
                                    "aws:SourceArn": rule_arns[0] if len(rule_arns) == 1 else rule_arns
                                },
                            },
                        ),
                        statement(
                            "sns:Publish",
                            [ref("AlarmsTopic"), ref("ReportsTopic")],
                            Principal={"Service": "cloudwatch.amazonaws.com"},
                            Condition={
                                "StringEquals": {"aws:SourceAccount": spec["account_id"]},
                                "ArnLike": {
                                    "aws:SourceArn": f"arn:aws:cloudwatch:{region}:{spec['account_id']}:alarm:{prefix(spec)}-*"
                                },
                            },
                        ),
                    ],
                },
            },
        )
        for group in log_groups(spec):
            r["Log" + digest(group)[:16]] = resource(
                "Logs::LogGroup",
                {"LogGroupName": group, "RetentionInDays": spec["log_retention_days"], "Tags": tagged(spec)},
                retain=True,
            )
        for instance in spec["instances"]:
            if not instance["nginx_alarm"]:
                continue
            for kind in ("access", "error"):
                group = f"{log_prefix(spec)}/{instance['id']}/nginx-{kind}"
                r["Filter" + digest([instance["id"], kind])[:16]] = resource(
                    "Logs::MetricFilter",
                    {
                        "FilterName": name(spec, f"{instance['id']}-{kind}"),
                        "LogGroupName": group,
                        "FilterPattern": spec["nginx_filters"][kind]["pattern"],
                        "MetricTransformations": [
                            {
                                "MetricName": (
                                    f"nginx-{'failed-requests' if kind == 'access' else 'diagnostic-events'}-{instance['id']}"
                                    if "observability" in spec
                                    else f"nginx-upstream-errors-{instance['id']}"
                                ),
                                "MetricNamespace": f"{spec['project']}/{spec['environment']}/Nginx",
                                "MetricValue": "1",
                                "DefaultValue": 0,
                            }
                        ],
                    },
                    depends=["Log" + digest(group)[:16]],
                )
    return t


def add_function(t, spec, logical, function, artifact, env, region, policies, timeout):
    if len(json.dumps(env, separators=(",", ":"), ensure_ascii=False).encode()) > 4096:
        from infra.deploy.verify import VerificationError

        raise VerificationError("Lambda environment exceeds the conservative 4 KiB rendering limit")
    r = t["Resources"]
    physical = name(spec, function.replace("_", "-"), release=True)
    group = f"/aws/lambda/{physical}"
    r[logical + "Log"] = resource(
        "Logs::LogGroup",
        {"LogGroupName": group, "RetentionInDays": spec["log_retention_days"], "Tags": tagged(spec, True)},
        retain=True,
    )
    log_arn = f"arn:aws:logs:{region}:{spec['account_id']}:log-group:{group}:*"
    r[logical + "Role"] = role(
        spec,
        "lambda.amazonaws.com",
        [statement(["logs:CreateLogStream", "logs:PutLogEvents"], log_arn), *policies],
        release=True,
    )
    r[logical] = resource(
        "Lambda::Function",
        {
            "FunctionName": physical,
            "Runtime": "python3.12",
            "Architectures": ["x86_64"],
            "Handler": "lambda_function.lambda_handler",
            "Code": {
                "S3Bucket": artifact["bucket"],
                "S3Key": artifact["key"],
                "S3ObjectVersion": artifact["version_id"],
            },
            "Role": att(logical + "Role"),
            "Timeout": timeout,
            "MemorySize": 256,
            "Environment": {"Variables": env},
            "Tags": tagged(spec, True),
        },
        retain=True,
        depends=[logical + "Log"],
    )
    r[logical + "Version"] = resource(
        "Lambda::Version",
        {
            "FunctionName": ref(logical),
            "CodeSha256": base64.b64encode(bytes.fromhex(artifact["sha256"])).decode(),
            "Description": f"{spec['release_id']} code/config snapshot",
        },
        retain=True,
    )
    t["Outputs"][logical + "VersionArn"] = {"Value": ref(logical + "Version")}
    t["Outputs"][logical + "FunctionArn"] = {"Value": att(logical)}
    return ref(logical + "Version")


def tools_release(spec, artifacts, secret):
    region = spec["bedrock_region"]
    t = template(spec, region, "Tracaus create-only tools for code-owned orchestration")
    common = {
        "MONITOR_REGION": spec["monitor_region"],
        "ENVIRONMENT": spec["environment"],
        "EXPECTED_ACCOUNT_ID": spec["account_id"],
        "LOG_GROUP_PREFIX": log_prefix(spec),
        "ALLOWED_INSTANCE_IDS": ",".join(targets(spec)),
        "LOG_SCOPE_FILE": "config/log-scope.json",
        **({"ECS_TARGETS_FILE": "config/ecs-targets.json"} if ecs_services(spec) else {}),
    }
    logs_env = {
        **common,
        "LOG_CURSOR_SECRET_ARN": secret["arn"],
        "LOG_CURSOR_SECRET_VERSION": secret["version_id"],
        **(
            {"EXISTING_LOG_GROUPS_FILE": "config/existing-log-groups.json"}
            if existing_log_groups(spec)
            else {}
        ),
    }
    logs_arns = [log_group_arn(spec, group) for group in readable_log_groups(spec)]
    region_condition = {"StringEquals": {"aws:RequestedRegion": spec["monitor_region"]}}
    add_function(
        t,
        spec,
        "Logs",
        "fetch_logs",
        artifacts["fetch_logs"],
        logs_env,
        region,
        [
            statement(["logs:StartQuery"], logs_arns, Condition=region_condition),
            statement(
                ["logs:GetQueryResults", "logs:StopQuery", "logs:DescribeLogGroups"],
                "*",
                Condition=region_condition,
            ),
            statement("secretsmanager:GetSecretValue", secret["arn"]),
        ],
        120,
    )
    add_function(
        t,
        spec,
        "Metrics",
        "fetch_metrics",
        artifacts["fetch_metrics"],
        common,
        region,
        [
            statement("cloudwatch:GetMetricStatistics", "*", Condition=region_condition),
        ],
        30,
    )
    if ecs_services(spec):
        region_only = {"StringEquals": {"aws:RequestedRegion": spec["monitor_region"]}}
        monitor, account = spec["monitor_region"], spec["account_id"]
        add_function(
            t,
            spec,
            "Ecs",
            "fetch_ecs",
            artifacts["fetch_ecs"],
            common,
            region,
            [
                statement(
                    "ecs:DescribeServices",
                    [
                        f"arn:aws:ecs:{monitor}:{account}:service/{item['cluster']}/{item['service']}"
                        for item in ecs_services(spec)
                    ]
                    # A service created before long ARNs has the short form, without the cluster.
                    + sorted(
                        {
                            f"arn:aws:ecs:{monitor}:{account}:service/{item['service']}"
                            for item in ecs_services(spec)
                        }
                    ),
                ),
                statement(
                    ["ecs:ListTasks", "ecs:DescribeTasks"],
                    "*",
                    Condition={
                        "ArnEquals": {
                            "ecs:cluster": sorted({e["cluster_arn"] for e in ecs_targets(spec).values()})
                        },
                        **region_only,
                    },
                ),
                statement("ecs:DescribeTaskDefinition", "*", Condition=region_only),
                *(
                    [statement("elasticloadbalancing:DescribeTargetHealth", "*", Condition=region_only)]
                    if any(item["load_balancer"] for item in ecs_services(spec))
                    else []
                ),
            ],
            30,
        )
    return t


def alarm_owner(spec, alarm):
    """Who owns the service an alarm watches. Only observed EC2 services have one."""
    return alarm.get("owner") or next(
        (s["owner"] for s in spec["observability"]["services"] if s["instance_id"] == alarm["instance_id"]),
        None,
    )


def service_routing(spec):
    t = template(
        spec,
        spec["monitor_region"],
        "Tracaus active routing; apply only after candidate and coverage verification",
    )
    r = t["Resources"]
    # Endpoint-specific logical IDs make retirement explicit. Reconciliation removes the old recipient first.
    for address in recipients(spec):
        r["Email" + digest(address)[:16]] = resource(
            "SNS::Subscription",
            {"Protocol": "email", "Endpoint": address, "TopicArn": topic_arn(spec, "reports")},
        )
    if spec["instances"]:
        r["Ec2Down"] = resource(
            "Events::Rule",
            {
                "Name": name(spec, "ec2-down"),
                "State": "DISABLED" if spec["maintenance_mode"] else "ENABLED",
                "EventPattern": {
                    "source": ["aws.ec2"],
                    "detail-type": ["EC2 Instance State-change Notification"],
                    "detail": {
                        "state": ["stopped", "terminated"],
                        "instance-id": [i["id"] for i in spec["instances"]],
                    },
                },
                "Targets": [{"Id": "alarms", "Arn": topic_arn(spec, "alarms")}],
                "Tags": tagged(spec),
            },
        )
    if ecs_services(spec):
        r["EcsDown"] = resource(
            "Events::Rule",
            {
                "Name": name(spec, "ecs-down"),
                "State": "DISABLED" if spec["maintenance_mode"] else "ENABLED",
                "EventPattern": {
                    "source": ["aws.ecs"],
                    "detail-type": ["ECS Task State Change"],
                    "detail": {
                        "lastStatus": ["STOPPED"],
                        "clusterArn": sorted({item["cluster_arn"] for item in ecs_targets(spec).values()}),
                        "group": sorted({f"service:{item['service']}" for item in ecs_services(spec)}),
                    },
                },
                "Targets": [{"Id": "alarms", "Arn": topic_arn(spec, "alarms")}],
                "Tags": tagged(spec),
            },
        )
    for alarm in alarm_descriptors(spec):
        r["Alarm" + digest(alarm["id"])[:16]] = resource(
            "CloudWatch::Alarm",
            {
                "AlarmName": alarm["alarm_name"],
                **(
                    {"AlarmDescription": f"Owner: {owner}; docs/OPERATE.md"}
                    if "observability" in spec and (owner := alarm_owner(spec, alarm))
                    else {}
                ),
                "Namespace": alarm["namespace"],
                "MetricName": alarm["metric_name"],
                "Dimensions": [{"Name": k, "Value": v} for k, v in sorted(alarm["dimensions"].items())],
                "Statistic": alarm["statistic"],
                "Period": alarm["period"],
                "EvaluationPeriods": 2,
                "DatapointsToAlarm": 2,
                "Threshold": alarm["threshold"],
                "ComparisonOperator": alarm["comparison"],
                "TreatMissingData": alarm.get(
                    "missing_data", "notBreaching" if alarm["namespace"].endswith("/Nginx") else "missing"
                ),
                "ActionsEnabled": not spec["maintenance_mode"]
                and (not alarm["namespace"].endswith("/Health") or spec["observability"]["enabled"]),
                "AlarmActions": [topic_arn(spec, "alarms")],
                **({"OKActions": [topic_arn(spec, "alarms")]} if "observability" in spec else {}),
                "Tags": tagged(spec),
            },
        )
    return t


TEMPLATE_LIMIT = 51200  # bytes CloudFormation accepts in an inline TemplateBody


def template_body(t):
    """The exact text sent to CloudFormation: compact, so the size guard measures what is sent."""
    return json.dumps(t, sort_keys=True, separators=(",", ":"))


def template_hash(t):
    return hashlib.sha256(template_body(t).encode()).hexdigest()
