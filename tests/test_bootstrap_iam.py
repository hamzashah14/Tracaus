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
        conditions = statement.get("Condition", {}).get("StringEquals", {})
        if any(context.get(key) != value for key, value in conditions.items()):
            continue
        return True
    return False


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
