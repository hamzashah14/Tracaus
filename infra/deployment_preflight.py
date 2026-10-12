"""Read-only deployment prerequisite checks; simulation is not effective authorization."""

import json

from botocore.exceptions import ClientError

from infra.spec import ecs_services, prefix
from infra.templates import bucket_name
from infra.verify import VerificationError

# Creation APIs frequently require '*'; these checks deliberately expose that
# requirement instead of supplying AdministratorAccess or changing customer IAM.
RESOURCE_ACTIONS = {
    "s3": [
        "CreateBucket",
        "PutBucketVersioning",
        "PutEncryptionConfiguration",
        "PutBucketPublicAccessBlock",
        "PutBucketPolicy",
        "PutLifecycleConfiguration",
        "PutBucketTagging",
        "GetBucketLocation",
        "GetBucketVersioning",
    ],
    "sns": ["CreateTopic", "SetTopicAttributes", "Subscribe", "GetTopicAttributes", "TagResource"],
    "sqs": ["CreateQueue", "SetQueueAttributes", "GetQueueAttributes", "TagQueue"],
    "dynamodb": [
        "CreateTable",
        "DescribeTable",
        "UpdateTimeToLive",
        "UpdateContinuousBackups",
        "TagResource",
    ],
    "lambda": [
        "CreateFunction",
        "PublishVersion",
        "GetFunction",
        "GetFunctionConfiguration",
        "PutFunctionConcurrency",
        "AddPermission",
        "CreateEventSourceMapping",
        "UpdateEventSourceMapping",
        "TagResource",
    ],
    "logs": ["CreateLogGroup", "PutRetentionPolicy", "PutMetricFilter", "TagResource"],
    "events": ["PutRule", "PutTargets", "DescribeRule", "TagResource"],
    "cloudwatch": ["PutMetricAlarm", "PutDashboard", "TagResource"],
    "secretsmanager": ["CreateSecret", "DescribeSecret", "GetRandomPassword", "TagResource"],
    "iam": ["CreateRole", "GetRole", "PutRolePolicy", "TagRole"],
    "kms": ["CreateKey", "CreateAlias", "PutKeyPolicy", "EnableKeyRotation", "DescribeKey", "TagResource"],
}


def permission_requests(spec, config):
    """Explicit scoped screening matrix, not a generated deployment policy."""
    account, project = spec["account_id"], prefix(spec)
    operator = spec["ci_principal_arn"]
    execution = spec["deployment_role_arn"]
    checks = []

    def add(principal, region, actions, resource, context=None):
        checks.append(
            {
                "principal": principal,
                "region": region,
                "actions": actions,
                "resource": resource,
                "context": context or {},
            }
        )

    for region in sorted({spec["monitor_region"], spec["bedrock_region"]}):
        add(
            operator,
            region,
            [
                "cloudformation:" + a
                for a in (
                    "CreateChangeSet",
                    "DescribeChangeSet",
                    "GetTemplate",
                    "ExecuteChangeSet",
                    "DescribeStacks",
                    "DescribeStackResource",
                    "SetStackPolicy",
                    "GetStackPolicy",
                    "UpdateTerminationProtection",
                )
            ],
            f"arn:aws:cloudformation:{region}:{account}:stack/{project}-*/*",
        )
        add(
            operator,
            region,
            ["iam:PassRole"],
            execution,
            {"iam:PassedToService": "cloudformation.amazonaws.com"},
        )
        resources = {
            "s3": f"arn:aws:s3:::{project}-{account}-{region}-*",
            "sns": f"arn:aws:sns:{region}:{account}:{project}-*",
            "sqs": f"arn:aws:sqs:{region}:{account}:{project}-*",
            "dynamodb": f"arn:aws:dynamodb:{region}:{account}:table/{project}-*",
            "lambda": f"arn:aws:lambda:{region}:{account}:function:{project}-*",
            "logs": f"arn:aws:logs:{region}:{account}:log-group:*",
            "events": f"arn:aws:events:{region}:{account}:rule/{project}-*",
            "cloudwatch": "*",
            "secretsmanager": f"arn:aws:secretsmanager:{region}:{account}:secret:{project}/*",
            "iam": f"arn:aws:iam::{account}:role/{spec['project']}/{spec['environment']}/*",
            "kms": "*",
        }
        for service, actions in RESOURCE_ACTIONS.items():
            if region != spec["monitor_region"] and service not in {
                "s3",
                "lambda",
                "logs",
                "secretsmanager",
                "iam",
            }:
                continue
            for action in actions:
                resource = resources[service]
                if action in {
                    "CreateKey",
                    "GetRandomPassword",
                    "CreateEventSourceMapping",
                    "UpdateEventSourceMapping",
                }:
                    resource = "*"
                add(execution, region, [f"{service}:{action}"], resource)
        for service in ("lambda.amazonaws.com", "bedrock-agentcore.amazonaws.com"):
            if service.startswith("bedrock") and config["runtime_target"] != "agentcore":
                continue
            add(execution, region, ["iam:PassRole"], resources["iam"], {"iam:PassedToService": service})
        # Same-region deployments use both separately named buckets.
        for purpose in ("tools", "monitor"):
            if region != spec["bedrock_region" if purpose == "tools" else "monitor_region"]:
                continue
            bucket = bucket_name(spec, purpose)
            add(
                operator,
                region,
                ["s3:PutObject", "s3:GetObjectVersion"],
                f"arn:aws:s3:::{bucket}/releases/{spec['release_id']}/*",
            )
        add(operator, region, ["lambda:InvokeFunction"], resources["lambda"] + ":*")
        add(
            operator,
            region,
            ["secretsmanager:DescribeSecret", "secretsmanager:UpdateSecretVersionStage"],
            resources["secretsmanager"],
        )
        if config["runtime_target"] == "agentcore" and region == spec["bedrock_region"]:
            add(
                execution,
                region,
                [
                    "bedrock-agentcore:" + a
                    for a in (
                        "CreateAgentRuntime",
                        "GetAgentRuntime",
                        "CreateAgentRuntimeEndpoint",
                        "GetAgentRuntimeEndpoint",
                        "TagResource",
                    )
                ],
                "*",
            )
    # Verification needs reads as well as create/write permission. These are
    # scoped screens; data/resource conditions may require customer context.
    reads = {
        "iam": ["GetRole", "ListRolePolicies", "GetRolePolicy", "ListAttachedRolePolicies"],
        "lambda": [
            "GetFunction",
            "GetFunctionConfiguration",
            "GetFunctionConcurrency",
            "GetAccountSettings",
            "ListEventSourceMappings",
        ],
        "dynamodb": ["DescribeTable", "DescribeTimeToLive", "DescribeContinuousBackups"],
        "sns": ["ListSubscriptionsByTopic", "GetSubscriptionAttributes", "GetTopicAttributes"],
        "sqs": ["GetQueueAttributes"],
        "logs": ["DescribeLogGroups", "TestMetricFilter"],
        "cloudwatch": ["ListMetrics", "DescribeAlarms"],
        "events": ["DescribeRule", "ListTargetsByRule"],
        "ec2": ["DescribeInstances", "DescribeRegions"],
        "s3": ["GetBucketVersioning", "GetBucketLocation"],
        "kms": ["DescribeKey", "GetKeyPolicy"],
    }
    if ecs_services(spec):
        reads["ecs"] = ["DescribeServices"]
        if any(item["load_balancer"] for item in ecs_services(spec)):
            reads["elasticloadbalancing"] = ["DescribeTargetGroups"]
    for service, actions in reads.items():
        region = spec["monitor_region"]
        scoped = {
            "iam": f"arn:aws:iam::{account}:role/{spec['project']}/{spec['environment']}/*",
            "lambda": f"arn:aws:lambda:{region}:{account}:function:{project}-*",
            "dynamodb": f"arn:aws:dynamodb:{region}:{account}:table/{project}-*",
            "sns": f"arn:aws:sns:{region}:{account}:{project}-*",
            "sqs": f"arn:aws:sqs:{region}:{account}:{project}-*",
            "events": f"arn:aws:events:{region}:{account}:rule/{project}-*",
            "s3": f"arn:aws:s3:::{project}-{account}-{region}-*",
        }
        for action in actions:
            resource = (
                "*"
                if action in {"GetAccountSettings", "ListEventSourceMappings", "GetSubscriptionAttributes"}
                else scoped.get(service, "*")
            )
            add(operator, region, [service + ":" + action], resource)
        if spec["bedrock_region"] != region and service in {"lambda", "iam", "logs", "s3"}:
            for action in actions:
                resource = scoped.get(service, "*").replace(region, spec["bedrock_region"])
                if action in {"GetAccountSettings", "ListEventSourceMappings"}:
                    resource = "*"
                add(operator, spec["bedrock_region"], [service + ":" + action], resource)
    return checks


def simulate(iam, request):
    entries = {"aws:RequestedRegion": request["region"], **request["context"]}
    args = {
        "PolicySourceArn": request["principal"],
        "ActionNames": request["actions"],
        "ResourceArns": [request["resource"]],
        "ContextEntries": [
            {
                "ContextKeyName": k,
                "ContextKeyValues": v if isinstance(v, list) else [v],
                "ContextKeyType": "stringList" if isinstance(v, list) else "string",
            }
            for k, v in entries.items()
        ],
    }
    rows = []
    markers = set()
    while True:
        result = iam.simulate_principal_policy(**args)
        rows.extend(result["EvaluationResults"])
        if not result.get("IsTruncated"):
            break
        if not result.get("Marker") or result["Marker"] in markers:
            raise VerificationError("Permission simulation omitted continuation marker")
        markers.add(result["Marker"])
        if len(markers) > 100:
            raise VerificationError("Permission simulation exceeds bounded pagination")
        args["Marker"] = result["Marker"]
    if {r["EvalActionName"] for r in rows} != set(request["actions"]):
        raise VerificationError("Permission simulation omitted requested actions")
    return [
        {
            "principal": request["principal"],
            "action": r["EvalActionName"],
            "resource": request["resource"],
            "decision": r["EvalDecision"],
            "missing_context": r.get("MissingContextValues", []),
        }
        for r in rows
    ]


def check(plan, factory, *, remaining_reserved=None):
    spec, config = plan["spec"], plan["runtime_config"]
    if spec["reference_only"]:
        raise VerificationError("Synthetic configuration cannot check or deploy AWS")
    identity = factory("sts", spec["monitor_region"]).get_caller_identity()
    if identity["Account"] != spec["account_id"]:
        raise VerificationError("Wrong AWS account; no writes permitted")
    iam = factory("iam", spec["monitor_region"])
    for field in ("ci_principal_arn", "ui_principal_arn", "deployment_role_arn"):
        arn = spec[field]
        role = iam.get_role(RoleName=arn.rsplit("/", 1)[-1])["Role"]
        if role["Arn"] != arn:
            raise VerificationError("Configured IAM role is absent or has a different path")
        if field == "ci_principal_arn":
            wanted = f"arn:aws:sts::{spec['account_id']}:assumed-role/{role['RoleName']}/"
            if not identity["Arn"].startswith(wanted):
                raise VerificationError("Run with the configured CI/operator role credentials")
        if field == "deployment_role_arn":
            policy = role["AssumeRolePolicyDocument"]
            if isinstance(policy, str):
                policy = json.loads(policy)
            statements = policy.get("Statement", [])
            if not any(
                s.get("Effect") == "Allow"
                and s.get("Principal", {}).get("Service") == "cloudformation.amazonaws.com"
                and "sts:AssumeRole"
                in ([s["Action"]] if isinstance(s.get("Action"), str) else s.get("Action", []))
                for s in statements
            ):
                raise VerificationError("Deployment role does not explicitly trust CloudFormation")
    regions = factory("ec2", spec["monitor_region"]).describe_regions(AllRegions=True)["Regions"]
    enabled = {
        r["RegionName"] for r in regions if r.get("OptInStatus") in {"opt-in-not-required", "opted-in"}
    }
    if not {spec["monitor_region"], spec["bedrock_region"]} <= enabled:
        raise VerificationError("A selected AWS region is disabled")
    expected = {i["id"] for i in spec["instances"]}
    if expected:  # DescribeInstances with no IDs lists every instance in the account
        actual = factory("ec2", spec["monitor_region"]).describe_instances(InstanceIds=sorted(expected))
        active = {
            i["InstanceId"]
            for r in actual["Reservations"]
            for i in r["Instances"]
            if i["State"]["Name"] not in {"terminated", "shutting-down"}
        }
        if active != expected:
            raise VerificationError("Create/configure the declared EC2 inventory before deployment")
    from infra.verify import assert_concurrency, assert_ecs_inventory

    assert_ecs_inventory(spec, factory)

    requested = config["initial_reserved_concurrency"]
    assert_concurrency(
        factory("lambda", spec["monitor_region"]),
        requested if remaining_reserved is None else remaining_reserved,
    )
    model_api = spec.get("model_provider", "bedrock") == "model_api"
    if model_api:
        # Existence and a unique current version only; the key value is never read. A missing
        # secret is a blocker, but AccessDenied stays an error rather than being read as absence.
        secret_name = prefix(spec) + "/model-api-key"
        try:
            secret = factory("secretsmanager", spec["bedrock_region"]).describe_secret(SecretId=secret_name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
            raise VerificationError(
                f"Create the model API key secret {secret_name} before deploying"
            ) from None
        if sum("AWSCURRENT" in stages for stages in secret.get("VersionIdsToStages", {}).values()) != 1:
            raise VerificationError("Model API key secret has no unique current version")
    else:
        bedrock = factory("bedrock", spec["bedrock_region"])
        if any(
            ":inference-profile/" in arn or ":application-inference-profile/" in arn
            for arn in spec["model_arns"]
        ):
            model = bedrock.get_inference_profile(inferenceProfileIdentifier=spec["model_id"])
            if (
                model.get("inferenceProfileArn") not in spec["model_arns"]
                or not model.get("models")
                or any(item.get("modelArn") not in spec["model_arns"] for item in model["models"])
            ):
                raise VerificationError(
                    "Model profile and its destination models must match declared model_arns"
                )
        else:
            model = bedrock.get_foundation_model(modelIdentifier=spec["model_id"])
            if model.get("modelDetails", {}).get("modelArn") not in spec["model_arns"]:
                raise VerificationError("Model catalog ARN differs from declared model_arns")
    checks = []
    for request in permission_requests(spec, config):
        checks.extend(simulate(iam, request))
    denied = [r for r in checks if r["decision"] != "allowed" or r["missing_context"]]
    return {
        "status": "BLOCKED" if denied else "SIMULATED",
        "account_matches": True,
        "inventory_present": True,
        # None: the Bedrock catalog check does not apply to a model API.
        "model_catalog_visible": None if model_api else True,
        **({"model_secret_present": True} if model_api else {}),
        "permission_checks": checks,
        "blockers": denied,
        "limitations": [
            "Simulation is a preliminary screen, not effective authorization or a complete IAM policy generator",
            "Session policies, trust conditions, resource policies, organizational and endpoint controls can differ at execution",
            "Model entitlements, telemetry, delivery and real identity require runtime verification",
            *(
                ["Model API key value, endpoint reachability and quota are not read or called by this check"]
                if model_api
                else []
            ),
        ],
    }
