"""Render one CloudFormation template that creates the IAM roles the deployment tool needs.

An AWS administrator deploys it once. It makes the three roles named in `deployment.json`
(operator, CloudFormation execution, UI workload) and, on request, an instance role for the
CloudWatch agent. Permissions are scoped by the project-environment name prefix, so the
roles can only touch resources whose names start with it. The resource strings match the
permission screen in `infra.deploy.deployment_preflight`, so `check` can confirm the result.
"""

from infra.spec import ecs_services, prefix
from infra.stacks.templates import ref

CLOUDFORMATION = "cloudformation.amazonaws.com"
AGENTCORE = "bedrock-agentcore.amazonaws.com"


def parse_role(arn):
    """Return (path, name) of a role ARN: arn:aws:iam::ACCOUNT:role/PATH/NAME."""
    path, _, name = arn.partition(":role/")[2].rpartition("/")
    return f"/{path}/" if path else "/", name


def allow(actions, resources, condition=None):
    statement = {"Effect": "Allow", "Action": sorted(actions), "Resource": resources}
    if condition:
        statement["Condition"] = condition
    return statement


def regions(spec):
    return sorted({spec["monitor_region"], spec["bedrock_region"]})


def scoped(spec, template):
    """One resource string per region, e.g. template = 'arn:aws:sns:{region}:{account}:{prefix}-*'."""
    return [
        template.format(region=region, account=spec["account_id"], prefix=prefix(spec))
        for region in regions(spec)
    ]


def operator_policy(spec):
    account = spec["account_id"]
    iam_scope = f"arn:aws:iam::{account}:role/{spec['project']}/{spec['environment']}/*"
    return [
        allow(
            ["cloudformation:*"], scoped(spec, "arn:aws:cloudformation:{region}:{account}:stack/{prefix}-*/*")
        ),
        allow(
            ["iam:PassRole"],
            [spec["deployment_role_arn"]],
            {"StringEquals": {"iam:PassedToService": CLOUDFORMATION}},
        ),
        allow(
            [
                "s3:PutObject",
                "s3:GetObject",
                "s3:GetObjectVersion",
                "s3:ListBucket",
                "s3:GetBucketVersioning",
                "s3:GetBucketLocation",
            ],
            scoped(spec, "arn:aws:s3:::{prefix}-{account}-{region}-*")
            + scoped(spec, "arn:aws:s3:::{prefix}-{account}-{region}-*/*"),
        ),
        allow(
            ["lambda:InvokeFunction", "lambda:GetFunction", "lambda:GetFunctionConfiguration"]
            + ["lambda:GetFunctionConcurrency"],
            scoped(spec, "arn:aws:lambda:{region}:{account}:function:{prefix}-*"),
        ),
        allow(
            ["secretsmanager:DescribeSecret", "secretsmanager:UpdateSecretVersionStage"],
            scoped(spec, "arn:aws:secretsmanager:{region}:{account}:secret:{prefix}/*"),
        ),
        allow(
            ["iam:GetRole", "iam:ListRolePolicies", "iam:GetRolePolicy", "iam:ListAttachedRolePolicies"],
            [iam_scope, spec["ci_principal_arn"], spec["deployment_role_arn"], spec["ui_principal_arn"]],
        ),
        allow(
            ["iam:SimulatePrincipalPolicy"],
            [spec["ci_principal_arn"], spec["deployment_role_arn"]],
        ),
        # Read-only inspection of what the stacks created, plus the account checks `check` runs.
        allow(
            [
                "dynamodb:DescribeTable",
                "dynamodb:DescribeTimeToLive",
                "dynamodb:DescribeContinuousBackups",
                "sns:ListSubscriptionsByTopic",
                "sns:GetSubscriptionAttributes",
                "sns:GetTopicAttributes",
                "sqs:GetQueueAttributes",
                "logs:DescribeLogGroups",
                "logs:TestMetricFilter",
                "cloudwatch:ListMetrics",
                "cloudwatch:DescribeAlarms",
                "cloudwatch:GetDashboard",
                "events:DescribeRule",
                "events:ListTargetsByRule",
                "ec2:DescribeInstances",
                "ec2:DescribeRegions",
                "s3:GetBucketVersioning",
                "s3:GetBucketLocation",
                "kms:DescribeKey",
                "kms:GetKeyPolicy",
                "lambda:GetAccountSettings",
                "lambda:ListEventSourceMappings",
                "bedrock:GetFoundationModel",
                "bedrock:GetInferenceProfile",
                *(
                    [
                        "ecs:DescribeServices",
                        "ecs:DescribeTaskDefinition",
                        "elasticloadbalancing:DescribeTargetGroups",
                    ]
                    if ecs_services(spec)
                    else []
                ),
            ],
            ["*"],
        ),
    ]


def execution_policy(spec, agentcore):
    account = spec["account_id"]
    role_scope = f"arn:aws:iam::{account}:role/{spec['project']}/{spec['environment']}/*"
    statements = [
        allow(
            ["s3:*"],
            scoped(spec, "arn:aws:s3:::{prefix}-{account}-{region}-*")
            + scoped(spec, "arn:aws:s3:::{prefix}-{account}-{region}-*/*"),
        ),
        allow(["sns:*"], scoped(spec, "arn:aws:sns:{region}:{account}:{prefix}-*")),
        allow(["sqs:*"], scoped(spec, "arn:aws:sqs:{region}:{account}:{prefix}-*")),
        allow(["dynamodb:*"], scoped(spec, "arn:aws:dynamodb:{region}:{account}:table/{prefix}-*")),
        allow(["lambda:*"], scoped(spec, "arn:aws:lambda:{region}:{account}:function:{prefix}-*")),
        allow(["events:*"], scoped(spec, "arn:aws:events:{region}:{account}:rule/{prefix}-*")),
        allow(
            ["secretsmanager:*"], scoped(spec, "arn:aws:secretsmanager:{region}:{account}:secret:{prefix}/*")
        ),
        allow(
            [
                "logs:CreateLogGroup",
                "logs:DeleteLogGroup",
                "logs:DescribeLogGroups",
                "logs:PutRetentionPolicy",
                "logs:DeleteRetentionPolicy",
                "logs:PutMetricFilter",
                "logs:DeleteMetricFilter",
                "logs:TagResource",
                "logs:UntagResource",
                "logs:ListTagsForResource",
            ],
            scoped(spec, "arn:aws:logs:{region}:{account}:log-group:*"),
        ),
        allow(
            [
                "iam:CreateRole",
                "iam:DeleteRole",
                "iam:GetRole",
                "iam:UpdateRole",
                "iam:UpdateAssumeRolePolicy",
                "iam:PutRolePolicy",
                "iam:DeleteRolePolicy",
                "iam:GetRolePolicy",
                "iam:ListRolePolicies",
                "iam:ListAttachedRolePolicies",
                "iam:TagRole",
                "iam:UntagRole",
            ],
            [role_scope],
        ),
        allow(
            ["iam:PassRole"],
            [role_scope],
            {"StringEquals": {"iam:PassedToService": "lambda.amazonaws.com"}},
        ),
        # These APIs accept no resource-level scope, so they cannot be limited by name.
        allow(
            [
                "lambda:CreateEventSourceMapping",
                "lambda:UpdateEventSourceMapping",
                "lambda:DeleteEventSourceMapping",
                "lambda:GetEventSourceMapping",
                "lambda:ListEventSourceMappings",
                "lambda:GetAccountSettings",
                "secretsmanager:GetRandomPassword",
                "cloudwatch:PutMetricAlarm",
                "cloudwatch:DeleteAlarms",
                "cloudwatch:DescribeAlarms",
                "cloudwatch:PutDashboard",
                "cloudwatch:DeleteDashboards",
                "cloudwatch:GetDashboard",
                "cloudwatch:TagResource",
                "cloudwatch:UntagResource",
                "kms:CreateKey",
                "kms:CreateAlias",
                "kms:DeleteAlias",
                "kms:PutKeyPolicy",
                "kms:EnableKeyRotation",
                "kms:DescribeKey",
                "kms:GetKeyPolicy",
                "kms:GetKeyRotationStatus",
                "kms:ScheduleKeyDeletion",
                "kms:TagResource",
                "kms:UntagResource",
            ],
            ["*"],
        ),
    ]
    if agentcore:
        statements += [
            allow(
                [
                    "bedrock-agentcore:CreateAgentRuntime",
                    "bedrock-agentcore:UpdateAgentRuntime",
                    "bedrock-agentcore:DeleteAgentRuntime",
                    "bedrock-agentcore:GetAgentRuntime",
                    "bedrock-agentcore:CreateAgentRuntimeEndpoint",
                    "bedrock-agentcore:UpdateAgentRuntimeEndpoint",
                    "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
                    "bedrock-agentcore:GetAgentRuntimeEndpoint",
                    "bedrock-agentcore:TagResource",
                ],
                ["*"],
            ),
            allow(["iam:PassRole"], [role_scope], {"StringEquals": {"iam:PassedToService": AGENTCORE}}),
        ]
    return statements


def ui_principal_policy(spec):
    """The UI principal may assume only the role Tracaus generates for the UI."""
    return [
        allow(
            ["sts:AssumeRole"],
            [f"arn:aws:iam::{spec['account_id']}:role/{spec['project']}/{spec['environment']}/*"],
        )
    ]


def instance_policy(spec):
    """The managed CloudWatch agent policy plus a deny, so only Tracaus's stack creates log groups."""
    return [
        {
            "Effect": "Deny",
            "Action": ["logs:CreateLogGroup"],
            "Resource": [
                f"arn:aws:logs:*:{spec['account_id']}:log-group:/{spec['project']}/{spec['environment']}/*"
            ],
        }
    ]


def trust(principals):
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Principal": {"AWS": sorted(principals)}, "Action": "sts:AssumeRole"}
        ],
    }


def render(spec, *, operator_trust=None, ui_trust=None, agentcore=False, instance_role=False):
    """Return the template. Trust defaults to the account root: any identity in the account that
    an IAM policy lets call sts:AssumeRole. Pass explicit ARNs to narrow it."""
    root = f"arn:aws:iam::{spec['account_id']}:root"
    tags = [
        {"Key": "ManagedBy", "Value": "tracaus-iam-bootstrap"},
        {"Key": "Project", "Value": spec["project"]},
        {"Key": "Environment", "Value": spec["environment"]},
    ]

    def role(arn, assume, policy, name):
        path, role_name = parse_role(arn)
        return {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "RoleName": role_name,
                "Path": path,
                "AssumeRolePolicyDocument": assume,
                "Policies": [
                    {
                        "PolicyName": name,
                        "PolicyDocument": {"Version": "2012-10-17", "Statement": policy},
                    }
                ],
                "Tags": tags,
            },
        }

    resources = {
        "OperatorRole": role(
            spec["ci_principal_arn"],
            trust(operator_trust or [root]),
            operator_policy(spec),
            "tracaus-operator",
        ),
        "ExecutionRole": role(
            spec["deployment_role_arn"],
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": CLOUDFORMATION},
                        "Action": "sts:AssumeRole",
                    }
                ],
            },
            execution_policy(spec, agentcore),
            "tracaus-cloudformation-execution",
        ),
        "UiPrincipalRole": role(
            spec["ui_principal_arn"],
            trust(ui_trust or [root]),
            ui_principal_policy(spec),
            "tracaus-ui-principal",
        ),
    }
    outputs = {
        "OperatorRoleArn": {
            "Description": "ci_principal_arn",
            "Value": {"Fn::GetAtt": ["OperatorRole", "Arn"]},
        },
        "ExecutionRoleArn": {
            "Description": "deployment_role_arn",
            "Value": {"Fn::GetAtt": ["ExecutionRole", "Arn"]},
        },
        "UiPrincipalRoleArn": {
            "Description": "ui_principal_arn",
            "Value": {"Fn::GetAtt": ["UiPrincipalRole", "Arn"]},
        },
    }
    if instance_role:
        resources["InstanceRole"] = {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "RoleName": f"{prefix(spec)}-instance",
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "ec2.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                },
                "ManagedPolicyArns": ["arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"],
                "Policies": [
                    {
                        "PolicyName": "tracaus-no-log-group-creation",
                        "PolicyDocument": {"Version": "2012-10-17", "Statement": instance_policy(spec)},
                    }
                ],
                "Tags": tags,
            },
        }
        resources["InstanceProfile"] = {
            "Type": "AWS::IAM::InstanceProfile",
            "Properties": {"InstanceProfileName": f"{prefix(spec)}-instance", "Roles": [ref("InstanceRole")]},
        }
        outputs["InstanceProfileName"] = {
            "Description": "Attach this instance profile to every monitored EC2 instance",
            "Value": ref("InstanceProfile"),
        }
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": f"Tracaus IAM roles for {prefix(spec)}. Deploy once as an AWS administrator.",
        "Resources": resources,
        "Outputs": outputs,
    }
