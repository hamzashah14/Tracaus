"""The IAM bootstrap template: shape, least surprise, and coverage of the permission screen."""

import json
import re
import subprocess
import sys

import pytest

from infra.deploy import deployment_preflight
from infra.spec import load
from infra.stacks import bootstrap_iam
from tests.helpers import ROOT

SPEC = load(ROOT / "examples/deployment.example.json")
CONFIG = json.loads((ROOT / "examples/durable.example.json").read_text())
SPLIT = {**SPEC, "bedrock_region": "us-east-1"}


def matches(pattern, value, fold=False):
    """IAM wildcard matching: * is any run of characters, ? is one character."""
    expression = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    return re.fullmatch(expression, value, re.IGNORECASE if fold else 0) is not None


def listed(value):
    return [value] if isinstance(value, str) else value


def allowed(statements, action, resource, context):
    for statement in statements:
        if statement["Effect"] != "Allow":
            continue
        if not any(matches(a, action, fold=True) for a in listed(statement["Action"])):
            continue
        if not any(matches(r, resource) for r in listed(statement["Resource"])):
            continue
        if not conditions_hold(statement.get("Condition", {}), context):
            continue
        return True
    return False


def conditions_hold(condition, context):
    """StringEquals and StringLike against the request context. Any other operator fails the test loudly, so a
    new kind of condition is never silently ignored."""
    for operator, pairs in condition.items():
        for key, wanted in pairs.items():
            value = context.get(key)
            if value is None:
                return False
            if operator == "StringEquals":
                holds = value in listed(wanted)
            elif operator == "StringLike":
                holds = any(matches(pattern, value) for pattern in listed(wanted))
            else:
                raise AssertionError(f"This evaluator does not know {operator}")
            if not holds:
                return False
    return True


def policies(template):
    result = {}
    for logical, resource in template["Resources"].items():
        if resource["Type"] == "AWS::IAM::Role":
            statements = []
            for policy in resource["Properties"].get("Policies", []):
                statements += policy["PolicyDocument"]["Statement"]
            result[resource["Properties"].get("RoleName") or logical] = statements
    return result


@pytest.mark.parametrize("spec", [SPEC, SPLIT], ids=["same-region", "split-region"])
@pytest.mark.parametrize("agentcore", [False, True], ids=["standalone", "agentcore"])
def test_every_screened_permission_is_granted_to_the_right_role(spec, agentcore):
    config = {**CONFIG, "runtime_target": "agentcore" if agentcore else "standalone"}
    template = bootstrap_iam.render(spec, agentcore=agentcore)
    by_role = policies(template)
    names = {
        spec["ci_principal_arn"]: "customer-ci",
        spec["deployment_role_arn"]: "customer-cfn-deployment",
    }
    requests = deployment_preflight.permission_requests(spec, config)
    assert len(requests) > 80  # an empty screen would pass vacuously
    for request in requests:
        statements = by_role[names[request["principal"]]]
        for action in request["actions"]:
            assert allowed(statements, action, request["resource"], request["context"]), (
                request["principal"],
                action,
                request["resource"],
            )


def test_agentcore_permissions_exist_only_when_asked():
    plain = bootstrap_iam.render(SPEC)
    assert "bedrock-agentcore" not in json.dumps(plain)
    assert "bedrock-agentcore" in json.dumps(bootstrap_iam.render(SPEC, agentcore=True))


def test_roles_get_the_names_and_paths_of_the_configured_arns():
    spec = {**SPEC, "ci_principal_arn": "arn:aws:iam::123456789012:role/ops/team/customer-ci"}
    properties = bootstrap_iam.render(spec)["Resources"]["OperatorRole"]["Properties"]
    assert (properties["RoleName"], properties["Path"]) == ("customer-ci", "/ops/team/")
    plain = bootstrap_iam.render(SPEC)["Resources"]["ExecutionRole"]["Properties"]
    assert (plain["RoleName"], plain["Path"]) == ("customer-cfn-deployment", "/")


def test_trust_policies():
    template = bootstrap_iam.render(SPEC)
    resources = template["Resources"]
    execution = resources["ExecutionRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert execution == [
        {
            "Effect": "Allow",
            "Principal": {"Service": "cloudformation.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ]
    root = "arn:aws:iam::123456789012:root"
    for logical in ("OperatorRole", "UiPrincipalRole"):
        statement = resources[logical]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
        assert statement["Principal"] == {"AWS": [root]}
    narrow = bootstrap_iam.render(
        SPEC,
        operator_trust=["arn:aws:iam::123456789012:role/admin-b", "arn:aws:iam::123456789012:role/admin-a"],
        ui_trust=["arn:aws:iam::123456789012:user/ui"],
    )["Resources"]
    assert narrow["OperatorRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Principal"] == {
        "AWS": ["arn:aws:iam::123456789012:role/admin-a", "arn:aws:iam::123456789012:role/admin-b"]
    }
    ui = narrow["UiPrincipalRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
    assert ui["Principal"] == {"AWS": ["arn:aws:iam::123456789012:user/ui"]}


def test_no_role_is_an_administrator_and_dangerous_grants_are_absent():
    by_role = policies(bootstrap_iam.render(SPEC, agentcore=True))
    for name, statements in by_role.items():
        for statement in statements:
            assert statement["Effect"] == "Allow"
            actions = listed(statement["Action"])
            assert "*" not in actions and not any(a.endswith(":*") and a.startswith("iam:") for a in actions)
            if "*" in listed(statement["Resource"]):
                # Resource * is allowed only for actions that accept no resource scope or are read-only.
                assert not any(a.startswith(("s3:*", "iam:", "sts:")) for a in actions), name
    execution = by_role["customer-cfn-deployment"]
    for action in ("iam:AttachRolePolicy", "iam:CreateUser", "iam:CreateAccessKey", "sts:AssumeRole"):
        assert not allowed(execution, action, "arn:aws:iam::123456789012:role/tracaus/staging/x", {})
    operator = by_role["customer-ci"]
    for action in ("iam:CreateRole", "iam:PutRolePolicy", "s3:DeleteObject", "lambda:CreateFunction"):
        assert not allowed(operator, action, "arn:aws:iam::123456789012:role/tracaus/staging/x", {})
    # Roles outside the project path and resources outside the name prefix stay out of reach.
    assert not allowed(execution, "iam:CreateRole", "arn:aws:iam::123456789012:role/other/staging/x", {})
    assert not allowed(execution, "s3:CreateBucket", "arn:aws:s3:::unrelated-bucket", {})
    assert not allowed(
        execution, "lambda:CreateFunction", "arn:aws:lambda:eu-central-1:123456789012:function:other", {}
    )
    # PassRole needs the matching service.
    role = "arn:aws:iam::123456789012:role/tracaus/staging/x"
    assert allowed(execution, "iam:PassRole", role, {"iam:PassedToService": "lambda.amazonaws.com"})
    assert not allowed(execution, "iam:PassRole", role, {"iam:PassedToService": "ec2.amazonaws.com"})
    assert not allowed(execution, "iam:PassRole", role, {})


def test_ui_principal_may_only_assume_tracaus_generated_role():
    statements = policies(bootstrap_iam.render(SPEC))["customer-ui"]
    assert allowed(statements, "sts:AssumeRole", "arn:aws:iam::123456789012:role/tracaus/staging/Ui-1", {})
    assert not allowed(statements, "sts:AssumeRole", "arn:aws:iam::123456789012:role/customer-ci", {})
    assert not allowed(
        statements, "s3:GetObject", "arn:aws:s3:::tracaus-staging-123456789012-eu-central-1-monitor/x", {}
    )


def test_instance_role_is_optional_and_cannot_create_log_groups():
    assert "InstanceRole" not in bootstrap_iam.render(SPEC)["Resources"]
    template = bootstrap_iam.render(SPEC, instance_role=True)
    role = template["Resources"]["InstanceRole"]["Properties"]
    assert role["ManagedPolicyArns"] == ["arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"]
    deny = role["Policies"][0]["PolicyDocument"]["Statement"][0]
    assert deny["Effect"] == "Deny" and deny["Action"] == ["logs:CreateLogGroup"]
    assert deny["Resource"] == ["arn:aws:logs:*:123456789012:log-group:/tracaus/staging/*"]
    assert template["Resources"]["InstanceProfile"]["Properties"]["Roles"] == [{"Ref": "InstanceRole"}]
    assert template["Outputs"]["InstanceProfileName"]["Value"] == {"Ref": "InstanceProfile"}


def test_outputs_name_the_three_deployment_fields_and_policies_fit_aws_limits():
    template = bootstrap_iam.render(SPEC, agentcore=True, instance_role=True)
    assert [
        template["Outputs"][k]["Description"] for k in sorted(template["Outputs"]) if k.endswith("RoleArn")
    ] == [
        "deployment_role_arn",
        "ci_principal_arn",
        "ui_principal_arn",
    ]
    for resource in template["Resources"].values():
        for policy in resource["Properties"].get("Policies", []):
            assert len(json.dumps(policy["PolicyDocument"], separators=(",", ":"))) < 10240


def test_cli_writes_the_template_and_prints_the_deploy_command(tmp_path):
    output = tmp_path / "iam.template.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "infra",
            "bootstrap-iam",
            "--spec",
            str(ROOT / "examples/deployment.example.json"),
            "--output",
            str(output),
            "--instance-role",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text())["Resources"]["InstanceRole"]
    assert "CAPABILITY_NAMED_IAM" in result.stdout and "tracaus-staging-iam" in result.stdout


def test_cli_refuses_an_invalid_spec_without_echoing_values(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"account_id": "SECRET-VALUE"}))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "infra",
            "bootstrap-iam",
            "--spec",
            str(bad),
            "--output",
            str(tmp_path / "x.json"),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 1 and "SECRET-VALUE" not in result.stderr + result.stdout
    assert not (tmp_path / "x.json").exists()


# ---- the permissions boundary and the scoped execution role --------------------------------------------------


def every_stage():
    """Every rendered template of every example deployment: EC2, observers, ECS, a model API, both runtime
    targets, one region and two."""
    from scripts.dev import validate_infrastructure, validate_observations
    from scripts.dev.validate_durable import examples

    model_api = json.loads((ROOT / "examples/deployment.example.json").read_text())
    model_api.pop("model_arns")
    model_api.update(
        model_provider="model_api", model_api={"protocol": "openai", "base_url": "https://api.example.com/v1"}
    )
    specs = {
        "ec2": SPEC,
        "observers": load(ROOT / "examples/observability.example.json"),
        "ecs": load(ROOT / "examples/ecs.example.json"),
        "model-api": model_api,
    }
    stages = []
    for name, base in specs.items():
        for region in (base["bedrock_region"], "us-east-1"):
            spec = {**base, "bedrock_region": region}
            for target in ("standalone", "agentcore"):
                if name == "model-api" and target == "agentcore":
                    continue  # a model API runs only in the standalone runtime
                config = {**CONFIG, "runtime_target": target}
                rendered = (
                    validate_observations.fixtures(spec, config)
                    if "observability" in spec
                    else examples(spec, config)
                )
                stages.append((f"{name}-{region}-{target}", spec, rendered))
            stages.append(
                (
                    f"{name}-{region}-infra",
                    spec,
                    validate_infrastructure.examples({**spec, "reference_only": False}),
                )
            )
    return stages


def role_grants(template):
    for logical, resource in template["Resources"].items():
        if resource["Type"] != "AWS::IAM::Role":
            continue
        for policy in resource["Properties"].get("Policies", []):
            for statement in policy["PolicyDocument"]["Statement"]:
                yield logical, resource, statement


def test_every_role_a_stack_creates_carries_the_boundary():
    from infra.spec import boundary_arn

    seen = 0
    for label, spec, rendered in every_stage():
        for stage, template in rendered.items():
            for logical, resource in template["Resources"].items():
                if resource["Type"] == "AWS::IAM::Role":
                    seen += 1
                    assert resource["Properties"]["PermissionsBoundary"] == boundary_arn(spec), (
                        label,
                        stage,
                        logical,
                    )
    assert seen > 100  # a render that produced no roles would pass vacuously


def test_no_role_asks_for_more_than_the_boundary_allows():
    """The boundary is the union of what the functions need; a new permission in any stage must be added to it."""
    checked = 0
    for label, spec, rendered in every_stage():
        agentcore = label.endswith("agentcore")
        boundary = bootstrap_iam.boundary_policy(spec, agentcore or "agentcore" in label)
        context = {**bootstrap_iam.own_tags(spec, "aws:ResourceTag")}
        for stage, template in rendered.items():
            for logical, _, statement in role_grants(template):
                assert statement["Effect"] == "Allow"
                for action in listed(statement["Action"]):
                    for resource in listed(statement["Resource"]):
                        assert isinstance(resource, str), (label, stage, logical, action, resource)
                        wanted = {
                            **context,
                            **{
                                key: listed(value)[0]
                                for pairs in statement.get("Condition", {}).values()
                                for key, value in pairs.items()
                            },
                        }
                        assert allowed(boundary, action, resource, wanted), (
                            label,
                            stage,
                            logical,
                            action,
                            resource,
                        )
                        checked += 1
    assert checked > 1000


def test_the_boundary_is_a_managed_policy_within_its_size_limit_and_names_no_iam_or_sts():
    template = bootstrap_iam.render(SPEC, agentcore=True)
    policy = template["Resources"]["RuntimeBoundary"]
    assert policy["Type"] == "AWS::IAM::ManagedPolicy"
    assert policy["Properties"]["ManagedPolicyName"] == "tracaus-staging-runtime-boundary"
    assert template["Outputs"]["RuntimeBoundaryArn"]["Value"] == {"Ref": "RuntimeBoundary"}
    document = policy["Properties"]["PolicyDocument"]
    assert len(json.dumps(document, separators=(",", ":"))) < 6144  # IAM's limit for a managed policy
    for statement in document["Statement"]:
        assert statement["Effect"] == "Allow"
        assert not any(
            a.startswith(("iam:", "sts:", "organizations:", "ec2:")) for a in listed(statement["Action"])
        )
    from infra.spec import boundary_arn

    assert boundary_arn(SPEC).endswith("/" + "tracaus-staging-runtime-boundary")
    assert boundary_arn(SPEC).startswith("arn:aws:iam::123456789012:policy/tracaus/staging/")


def test_a_template_cannot_make_an_administrator_even_through_the_execution_role():
    """What S1 was: the execution role could create any role in its path with any inline policy."""
    execution = policies(bootstrap_iam.render(SPEC, agentcore=True))["customer-cfn-deployment"]
    role = "arn:aws:iam::123456789012:role/tracaus/staging/anything"
    boundary = "arn:aws:iam::123456789012:policy/tracaus/staging/tracaus-staging-runtime-boundary"
    for action in (
        "iam:CreateRole",
        "iam:PutRolePolicy",
        "iam:UpdateAssumeRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:DeleteRole",
        "iam:UpdateRole",
    ):
        assert allowed(execution, action, role, {"iam:PermissionsBoundary": boundary}), action
        assert not allowed(execution, action, role, {}), action  # no boundary, no change
        assert not allowed(
            execution, action, role, {"iam:PermissionsBoundary": "arn:aws:iam::aws:policy/x"}
        ), action
    for action in (
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:PutRolePermissionsBoundary",
        "iam:DeleteRolePermissionsBoundary",
        "iam:CreatePolicy",
        "iam:CreatePolicyVersion",
        "iam:SetDefaultPolicyVersion",
    ):
        assert not allowed(execution, action, role, {"iam:PermissionsBoundary": boundary}), action


def test_the_execution_role_cannot_touch_keys_log_groups_or_alarms_it_does_not_own():
    """What S2 was: key policy, log groups, alarms and mappings on * or log-group:*."""
    execution = policies(bootstrap_iam.render(SPEC, agentcore=True))["customer-cfn-deployment"]
    key = "arn:aws:kms:eu-central-1:123456789012:key/example-key"
    for action in ("kms:PutKeyPolicy", "kms:ScheduleKeyDeletion", "kms:CreateAlias", "kms:DeleteAlias"):
        assert not allowed(execution, action, key, bootstrap_iam.own_tags(SPEC, "aws:ResourceTag")), action
        assert not allowed(execution, action, "*", {}), action
    own = bootstrap_iam.own_tags(SPEC, "aws:RequestTag")
    assert allowed(execution, "kms:CreateKey", "*", own)
    assert not allowed(execution, "kms:CreateKey", "*", {})  # an untagged key is not ours
    assert not allowed(execution, "kms:CreateKey", "*", {**own, "aws:RequestTag/Project": "other"})
    assert allowed(execution, "kms:TagResource", key, bootstrap_iam.own_tags(SPEC, "aws:ResourceTag"))
    assert not allowed(execution, "kms:TagResource", key, {"aws:ResourceTag/Project": "other"})

    groups = "arn:aws:logs:eu-central-1:123456789012:log-group:"
    for action in ("logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:CreateLogGroup"):
        assert allowed(
            execution, action, groups + "/aws/lambda/tracaus-staging-example001-incident-ingress", {}
        )
        instance = SPEC["instances"][0]["id"]
        assert allowed(execution, action, groups + f"/tracaus/staging/{instance}/application", {})
        assert allowed(execution, action, groups + "/aws/bedrock-agentcore/runtimes/tracaus_staging_x-1", {})
        assert not allowed(execution, action, groups + "/prod/payments/audit", {}), action
        assert not allowed(execution, action, groups + "/aws/lambda/someone-elses-function", {}), action

    alarms = "arn:aws:cloudwatch:eu-central-1:123456789012:alarm:"
    for action in ("cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms"):
        assert allowed(execution, action, alarms + "tracaus-staging-i-0123456789abcdef0-cpu", {})
        assert not allowed(execution, action, alarms + "payments-high-latency", {}), action
        assert not allowed(execution, action, "*", {}), action

    own_function = {
        "lambda:FunctionArn": "arn:aws:lambda:eu-central-1:123456789012:function:tracaus-staging-x:1"
    }
    other_function = {"lambda:FunctionArn": "arn:aws:lambda:eu-central-1:123456789012:function:billing"}
    assert allowed(execution, "lambda:CreateEventSourceMapping", "*", own_function)
    assert not allowed(execution, "lambda:CreateEventSourceMapping", "*", other_function)
    mapping = "arn:aws:lambda:eu-central-1:123456789012:event-source-mapping:abc"
    assert allowed(execution, "lambda:DeleteEventSourceMapping", mapping, own_function)
    assert not allowed(execution, "lambda:DeleteEventSourceMapping", mapping, other_function)
