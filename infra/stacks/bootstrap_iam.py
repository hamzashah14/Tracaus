"""Render one CloudFormation template that creates the IAM roles the deployment tool needs.

An AWS administrator deploys it once. It makes the three roles named in `deployment.json`
(operator, CloudFormation execution, UI workload) and, on request, an instance role for the
CloudWatch agent. Permissions are scoped by the project-environment name prefix, so the
roles can only touch resources whose names start with it. The resource strings match the
permission screen in `infra.deploy.deployment_preflight`, so `check` can confirm the result.
"""

from infra.spec import boundary_arn, ecs_services, log_prefix, prefix, tags
from infra.stacks.templates import ref

CLOUDFORMATION = "cloudformation.amazonaws.com"
AGENTCORE_GROUPS = (
    "arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/{project}_{environment}_*"
)
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
        template.format(
            region=region,
            account=spec["account_id"],
            prefix=prefix(spec),
            project=spec["project"],
            environment=spec["environment"],
            log_prefix=log_prefix(spec),
        )
        for region in regions(spec)
    ]


def own_tags(spec, kind):
    """Tag conditions that name this deployment's own resources: kind is 'aws:RequestTag' or 'aws:ResourceTag'."""
    return {f"{kind}/{key}": value for key, value in tags(spec).items()}


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
    functions = scoped(spec, "arn:aws:lambda:{region}:{account}:function:{prefix}-*")
    statements = [
        allow(
            ["s3:*"],
            scoped(spec, "arn:aws:s3:::{prefix}-{account}-{region}-*")
            + scoped(spec, "arn:aws:s3:::{prefix}-{account}-{region}-*/*"),
        ),
        allow(["sns:*"], scoped(spec, "arn:aws:sns:{region}:{account}:{prefix}-*")),
        allow(["sqs:*"], scoped(spec, "arn:aws:sqs:{region}:{account}:{prefix}-*")),
        allow(["dynamodb:*"], scoped(spec, "arn:aws:dynamodb:{region}:{account}:table/{prefix}-*")),
        allow(["lambda:*"], functions),
        allow(["events:*"], scoped(spec, "arn:aws:events:{region}:{account}:rule/{prefix}-*")),
        allow(
            ["secretsmanager:*"], scoped(spec, "arn:aws:secretsmanager:{region}:{account}:secret:{prefix}/*")
        ),
        # Only the log groups the stacks create: the functions', the AgentCore runtimes' and Tracaus's own.
        allow(
            [
                "logs:CreateLogGroup",
                "logs:DeleteLogGroup",
                "logs:PutRetentionPolicy",
                "logs:DeleteRetentionPolicy",
                "logs:PutMetricFilter",
                "logs:DeleteMetricFilter",
                "logs:TagResource",
                "logs:UntagResource",
                "logs:ListTagsForResource",
            ],
            scoped(spec, "arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{prefix}-*")
            + (scoped(spec, AGENTCORE_GROUPS) if agentcore else [])
            + scoped(spec, "arn:aws:logs:{region}:{account}:log-group:{log_prefix}/*"),
        ),
        allow(["logs:DescribeLogGroups"], scoped(spec, "arn:aws:logs:{region}:{account}:log-group:*")),
        # Roles are created, changed and deleted only with the permissions boundary attached, so what a template
        # makes of them can never exceed that boundary. Attaching managed policies and changing the boundary
        # itself are not granted at all.
        allow(
            [
                "iam:CreateRole",
                "iam:PutRolePolicy",
                "iam:UpdateAssumeRolePolicy",
                "iam:DeleteRolePolicy",
                "iam:DeleteRole",
                "iam:UpdateRole",
            ],
            [role_scope],
            {"StringEquals": {"iam:PermissionsBoundary": boundary_arn(spec)}},
        ),
        allow(
            [
                "iam:GetRole",
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
        # Event source mappings have no name to scope by; the function they feed is the scope.
        allow(
            ["lambda:CreateEventSourceMapping"],
            ["*"],
            {"StringLike": {"lambda:FunctionArn": functions}},
        ),
        allow(
            [
                "lambda:UpdateEventSourceMapping",
                "lambda:DeleteEventSourceMapping",
                "lambda:GetEventSourceMapping",
            ],
            scoped(spec, "arn:aws:lambda:{region}:{account}:event-source-mapping:*"),
            {"StringLike": {"lambda:FunctionArn": functions}},
        ),
        allow(
            [
                "cloudwatch:PutMetricAlarm",
                "cloudwatch:DeleteAlarms",
                "cloudwatch:TagResource",
                "cloudwatch:UntagResource",
            ],
            scoped(spec, "arn:aws:cloudwatch:{region}:{account}:alarm:{prefix}-*"),
        ),
        allow(
            [
                "cloudwatch:PutDashboard",
                "cloudwatch:DeleteDashboards",
                "cloudwatch:TagResource",
                "cloudwatch:UntagResource",
            ],
            [f"arn:aws:cloudwatch::{account}:dashboard/{prefix(spec)}-*"],
        ),
        # A KMS key is created with this deployment's tags. Its policy is set when it is created and a key is
        # retained, never deleted by a stack, so no policy change or deletion is granted. TagResource at creation
        # cannot be told from tagging an existing key; it adds tags only.
        allow(
            ["kms:CreateKey", "kms:TagResource"],
            ["*"],
            {"StringEquals": own_tags(spec, "aws:RequestTag")},
        ),
        allow(
            ["kms:TagResource", "kms:UntagResource"],
            scoped(spec, "arn:aws:kms:{region}:{account}:key/*"),
            {"StringEquals": own_tags(spec, "aws:ResourceTag")},
        ),
        allow(
            ["kms:EnableKeyRotation", "kms:DescribeKey", "kms:GetKeyPolicy", "kms:GetKeyRotationStatus"],
            scoped(spec, "arn:aws:kms:{region}:{account}:key/*"),
        ),
        # These APIs accept no resource-level scope: reads, or a call that names nothing yet.
        allow(
            [
                "lambda:ListEventSourceMappings",
                "lambda:GetAccountSettings",
                "secretsmanager:GetRandomPassword",
                "cloudwatch:DescribeAlarms",
                "cloudwatch:GetDashboard",
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


def boundary_policy(spec, agentcore):
    """The most any role a release stack creates may do: what the Tracaus functions need, on this deployment's
    own resources. A role gets the smaller of this and its own policy, so a template that gives a role more
    (an administrator policy, say) gains nothing. tests/test_bootstrap_iam.py renders every role of every stack
    and fails if one asks for something outside it."""
    in_tag = {"StringEquals": own_tags(spec, "aws:ResourceTag")}
    statements = [
        allow(
            [
                "dynamodb:GetItem",
                "dynamodb:Query",
                "dynamodb:UpdateItem",
                "dynamodb:PutItem",
                "dynamodb:ConditionCheckItem",
                "dynamodb:DescribeStream",
                "dynamodb:GetRecords",
                "dynamodb:GetShardIterator",
                "dynamodb:ListStreams",
            ],
            scoped(spec, "arn:aws:dynamodb:{region}:{account}:table/{prefix}-*"),
        ),
        allow(
            ["s3:GetObjectVersion", "s3:PutObject"],
            scoped(spec, "arn:aws:s3:::{prefix}-{account}-{region}-*/*"),
        ),
        allow(
            ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:SendMessage"],
            scoped(spec, "arn:aws:sqs:{region}:{account}:{prefix}-*"),
        ),
        allow(
            ["sns:Publish", "sns:ListSubscriptionsByTopic", "sns:GetSubscriptionAttributes"],
            scoped(spec, "arn:aws:sns:{region}:{account}:{prefix}-*"),
        ),
        allow(
            ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"],
            scoped(spec, "arn:aws:kms:{region}:{account}:key/*"),
            in_tag,
        ),
        allow(
            ["lambda:InvokeFunction"], scoped(spec, "arn:aws:lambda:{region}:{account}:function:{prefix}-*")
        ),
        allow(
            ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            scoped(spec, "arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{prefix}-*")
            + (scoped(spec, AGENTCORE_GROUPS) if agentcore else [])
            + scoped(spec, "arn:aws:logs:{region}:{account}:log-group:{log_prefix}/audit*"),
        ),
        allow(
            ["logs:GetLogEvents"],
            scoped(spec, "arn:aws:logs:{region}:{account}:log-group:{log_prefix}/*"),
        ),
        allow(["logs:StartQuery"], scoped(spec, "arn:aws:logs:{region}:{account}:log-group:*")),
        allow(["logs:GetQueryResults", "logs:StopQuery", "logs:DescribeLogGroups"], ["*"]),
        allow(["cloudwatch:GetMetricStatistics"], ["*"]),
        allow(
            ["cloudwatch:PutMetricData"],
            ["*"],
            {"StringLike": {"cloudwatch:namespace": f"{spec['project']}/{spec['environment']}/*"}},
        ),
        allow(
            ["secretsmanager:GetSecretValue"],
            scoped(spec, "arn:aws:secretsmanager:{region}:{account}:secret:{prefix}/*")
            + scoped(spec, "arn:aws:secretsmanager:{region}:{account}:secret:{project}/{environment}/*"),
        ),
        allow(
            ["bedrock:InvokeModel", "bedrock:CountTokens"],
            ["arn:aws:bedrock:*::foundation-model/*", f"arn:aws:bedrock:*:{spec['account_id']}:*"],
        ),
        allow(
            [
                "cloudformation:DescribeChangeSet",
                "cloudformation:DescribeStackEvents",
                "cloudformation:DescribeStacks",
                "cloudformation:ListStackResources",
            ],
            [f"arn:aws:cloudformation:*:{spec['account_id']}:stack/{prefix(spec)}-*/*"],
        ),
    ]
    if ecs_services(spec):
        statements += [
            allow(["ecs:DescribeServices"], scoped(spec, "arn:aws:ecs:{region}:{account}:service/*")),
            allow(
                [
                    "ecs:ListTasks",
                    "ecs:DescribeTasks",
                    "ecs:DescribeTaskDefinition",
                    "elasticloadbalancing:DescribeTargetHealth",
                ],
                ["*"],
            ),
        ]
    if agentcore:
        statements.append(
            allow(
                ["bedrock-agentcore:InvokeAgentRuntime"],
                scoped(
                    spec, "arn:aws:bedrock-agentcore:{region}:{account}:runtime/{project}_{environment}_*"
                ),
            )
        )
    return statements


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
    resources["RuntimeBoundary"] = {
        "Type": "AWS::IAM::ManagedPolicy",
        "Properties": {
            "ManagedPolicyName": f"{prefix(spec)}-runtime-boundary",
            "Path": f"/{spec['project']}/{spec['environment']}/",
            "Description": "Permissions boundary for every role a Tracaus release stack creates",
            "PolicyDocument": {"Version": "2012-10-17", "Statement": boundary_policy(spec, agentcore)},
        },
    }
    outputs = {
        "RuntimeBoundaryArn": {
            "Description": "Permissions boundary the CloudFormation execution role requires on roles it creates",
            "Value": ref("RuntimeBoundary"),
        },
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
