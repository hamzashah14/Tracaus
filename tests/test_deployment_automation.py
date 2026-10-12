import copy
import json
import os
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from infra.deploy import automation, deployment_preflight, durable
from infra.deploy.verify import PendingConfirmation, VerificationError, routing_health
from infra.spec import name, prefix, topic_arn
from infra.stacks import owned_runtime
from infra.stacks.templates import service_routing
from scripts.dev.validate_durable import examples
from tests.helpers import ROOT


def configuration(
    tmp_path,
    target="standalone",
    observations=False,
    environment="staging",
    *,
    model_api=False,
):
    spec = json.loads(
        (
            ROOT
            / ("examples/observability.example.json" if observations else "examples/deployment.example.json")
        ).read_text()
    )
    spec["environment"] = environment
    if model_api:
        spec.pop("model_arns")
        spec["model_provider"] = "model_api"
        spec["model_api"] = {"protocol": "openai", "base_url": "https://models.example.invalid/v1"}
    config = json.loads((ROOT / "examples/durable.example.json").read_text())
    config["runtime_target"] = target
    (tmp_path / "deployment.json").write_text(json.dumps(spec))
    (tmp_path / "runtime.json").write_text(json.dumps(config))
    (tmp_path / "automation.json").write_text(
        json.dumps(
            {
                "version": 1,
                "spec": "deployment.json",
                "runtime_config": "runtime.json",
                "profile": None,
                "wheelhouse": str(ROOT / ".build/wheels"),
            }
        )
    )
    return automation.plan(tmp_path / "automation.json")


def test_dry_run_plan_no_aws_and_settings_change_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(automation, "factory", Mock(side_effect=AssertionError("AWS forbidden")))
    planned = configuration(tmp_path)
    assert planned["spec"]["reference_only"] and not planned["automatic_activation"]
    assert "AWS::SQS::Queue" in planned["resources"]["durable-foundation"]["resource_types"]
    assert "staging-canary" in planned["stages"]
    assert planned == automation.plan(tmp_path / "automation.json")
    p = tmp_path / "runtime.json"
    config = json.loads(p.read_text())
    config["retention_days"] += 1
    p.write_text(json.dumps(config))
    assert planned["plan_hash"] != automation.plan(tmp_path / "automation.json")["plan_hash"]


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "missing",
        "bad_profile",
        "unpaused",
    ],
)
def test_bad_settings_fail_without_clients(tmp_path, mutation):
    configuration(tmp_path)
    p = tmp_path / "automation.json"
    value = json.loads(p.read_text())
    if mutation == "unknown":
        value["credentials"] = "rejected"
    if mutation == "missing":
        del value["version"]
    if mutation == "bad_profile":
        value["profile"] = "../unsafe"
    if mutation == "unpaused":
        r = tmp_path / "runtime.json"
        config = json.loads(r.read_text())
        config["investigation_paused"] = False
        r.write_text(json.dumps(config))
    p.write_text(json.dumps(value))
    with pytest.raises((VerificationError, ValueError)):
        automation.plan(p)


@pytest.mark.parametrize(
    "extra",
    [
        {"identity": {"issuer": "https://login.example.invalid", "audience": "client"}},
        {"security": {}},
    ],
)
def test_runtime_config_with_removed_blocks_gets_one_clear_message(tmp_path, extra):
    from infra.deploy import durable
    from infra.deploy.verify import VerificationError
    from infra.spec import ROOT, load

    config = {**json.loads((ROOT / "examples/durable.example.json").read_text()), **extra}
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(config))
    with pytest.raises(VerificationError, match="Team sign-in changed"):
        durable.load_config(path, load(ROOT / "examples/deployment.example.json"))


def test_automation_json_with_initial_access_gets_the_team_signin_message(tmp_path):
    configuration(tmp_path)
    path = tmp_path / "automation.json"
    value = json.loads(path.read_text())
    value["initial_access"] = []
    path.write_text(json.dumps(value))
    with pytest.raises(VerificationError, match="Team sign-in changed"):
        automation.plan(path)


def test_private_state_atomic_and_lock(tmp_path):
    path = tmp_path / "state.json"
    automation.private_json(path, {"status": "WAITING"})
    assert path.stat().st_mode & 0o777 == 0o600
    with automation.locked(tmp_path):
        with pytest.raises(VerificationError, match="Another deployment"):
            with automation.locked(tmp_path):
                pass
    with automation.locked(tmp_path):
        pass
    link = tmp_path / "state.json.tmp"
    link.symlink_to(path)
    with pytest.raises(OSError):
        automation.private_json(path, {"status": "corrupt"})
    assert json.loads(path.read_text()) == {"status": "WAITING"}


@pytest.mark.parametrize("target", ["standalone", "agentcore"])
@pytest.mark.parametrize("observations", [False, True])
def test_order_and_separate_runtime_candidates(tmp_path, target, observations):
    p = configuration(tmp_path, target, observations)
    stages = p["stages"]
    assert (
        stages.index("owned-tools")
        < stages.index("durable-runtime")
        < stages.index("staging-canary")
        < stages.index("routing")
    )
    if target == "agentcore":
        assert stages.index("agentcore-runtime") < stages.index("agentcore-endpoint")
        assert stages.index("agentcore-endpoint") < stages.index("durable-runtime")
    if observations:
        assert stages.index("health-bootstrap") < stages.index("staging-canary")


def confirmed_subscriptions(spec, ingress_arn):
    """The SNS state a finished routing stage leaves behind, every email already clicked."""
    base = f"arn:aws:sns:{spec['monitor_region']}:{spec['account_id']}"
    subscriptions = [
        {
            "TopicArn": topic_arn(spec, "reports"),
            "Protocol": "email",
            "Endpoint": spec["notification_email"],
            "SubscriptionArn": f"{base}:{name(spec, 'reports')}:primary",
        },
        {
            "TopicArn": topic_arn(spec, "alarms"),
            "Protocol": "sqs",
            "Endpoint": ingress_arn,
            "SubscriptionArn": f"{base}:{name(spec, 'alarms')}:ingress",
        },
    ]
    if "observability" in spec:
        subscriptions.append(
            {
                "TopicArn": topic_arn(spec, "reports"),
                "Protocol": "sqs",
                "Endpoint": f"arn:aws:sqs:{spec['monitor_region']}:{spec['account_id']}:{name(spec, 'observation-receipts')}",
                "SubscriptionArn": f"{base}:{name(spec, 'reports')}:receipts",
            }
        )
    return subscriptions


def routing_clients(spec, subscriptions):
    """Synthetic SDK clients serving exactly the planned routing, with the given SNS subscriptions."""
    expected = service_routing(spec)["Resources"]
    rule = expected["Ec2Down"]["Properties"]
    events, sns, cloudwatch = Mock(), Mock(), Mock()
    events.describe_rule.return_value = {
        "State": rule["State"],
        "EventPattern": json.dumps(rule["EventPattern"]),
    }
    events.list_targets_by_rule.return_value = {"Targets": rule["Targets"]}
    sns.get_paginator.return_value.paginate.side_effect = lambda TopicArn: [
        {"Subscriptions": [s for s in subscriptions if s["TopicArn"] == TopicArn]}
    ]
    cloudwatch.describe_alarms.return_value = {
        "MetricAlarms": [
            copy.deepcopy(r["Properties"]) for r in expected.values() if r["Type"] == "AWS::CloudWatch::Alarm"
        ]
    }
    clients = {"events": events, "sns": sns, "cloudwatch": cloudwatch}
    return lambda service, region: clients[service]


class FakeDriver(automation.Driver):
    """Real staged renderer; synthetic SDK/build adapters only."""

    def __init__(self, planned, directory, monkeypatch):
        self.plan, self.directory = planned, directory
        spec, config = planned["spec"], planned["runtime_config"]
        # The example outputs (foundation, tools, versions) do not depend on the model provider.
        bedrock_spec = {k: v for k, v in spec.items() if k not in {"model_provider", "model_api"}}
        bedrock_spec.setdefault(
            "model_arns", json.loads((ROOT / "examples/deployment.example.json").read_text())["model_arns"]
        )
        _, self.outputs, artifacts, versions = examples(
            bedrock_spec,
            config,
            include_bindings=True,
        )
        self.outputs["versions"] = versions
        self.outputs["secret"] = {
            "arn": f"arn:aws:secretsmanager:{spec['bedrock_region']}:{spec['account_id']}:secret:tracaus-staging/log-cursor-123abc",
            "version_id": "a" * 32,
        }
        self.outputs["model_secret"] = {
            "arn": f"arn:aws:secretsmanager:{spec['bedrock_region']}:{spec['account_id']}:secret:{prefix(spec)}/model-api-key-123abc",
            "version_id": "a" * 32,
        }
        self.missing_model_secret = False
        self.artifacts = artifacts
        self.clients = Mock(side_effect=AssertionError("No live SDK"))
        self.preflight = {"status": "SIMULATED"}
        self.calls = []
        self.canaries = 0
        self.subscriptions = confirmed_subscriptions(spec, self.outputs["foundation"]["IngressQueueArn"])
        from infra.deploy import observations, release
        from scripts.build_lambdas import PIPELINE_FUNCTIONS

        def manifest(functions):
            return {"functions": {f: {"sha256": "b" * 64} for f in functions}}

        monkeypatch.setattr(
            durable,
            "checked_build",
            lambda path, **kw: manifest(kw.get("expected_functions", PIPELINE_FUNCTIONS)),
        )
        monkeypatch.setattr(release, "checked_build", lambda *a: manifest(("fetch_logs", "fetch_metrics")))
        monkeypatch.setattr(
            observations,
            "checked_build",
            lambda *a: manifest(("observation_probe", "observation_canary", "observation_receipt")),
        )
        monkeypatch.setattr(deployment_preflight, "check", lambda *a, **kw: self.preflight)

    def build(self):
        self.calls.append("build")

    def inspect_stack(self, *a):
        return None

    def stage(self, bundle, stage, record, save, **kw):
        assert stage in bundle["stages"]
        assert (
            record.get("template_hash", bundle["stages"][stage]["template_hash"])
            == bundle["stages"][stage]["template_hash"]
        )
        record.update(status="COMPLETE", template_hash=bundle["stages"][stage]["template_hash"])
        save()
        self.calls.append(stage)

    def op(self, bundle, command, **kw):
        spec = self.plan["spec"]
        stage = kw.get("stage")
        self.calls.append(command + (":" + stage if stage else ""))
        if command == "collect":
            if stage == "durable-foundation":
                return self.outputs["foundation"]
            if stage == "owned-tools":
                return self.outputs["tools"]
            if stage == "durable-runtime":
                return self.outputs["versions"]
            if stage == "observation-runtime":
                return {
                    logical
                    + "VersionArn": f"arn:aws:lambda:{spec['monitor_region']}:{spec['account_id']}:function:{name(spec, function.replace('_', '-'), True)}:1"
                    for function, logical in __import__(
                        "infra.stacks.observation_templates", fromlist=["FUNCTIONS"]
                    ).FUNCTIONS.items()
                }
            if stage == "routing":
                return {"UiRoleArn": f"arn:aws:iam::{spec['account_id']}:role/tracaus/staging/ui"}
            if stage.endswith("-runtime"):
                suffix = stage[:-8]
                runtime_id = name(spec, suffix, True).replace("-", "_") + "-1234567890"
                return {
                    "RuntimeId": runtime_id,
                    "RuntimeVersion": "1",
                    "RuntimeArn": f"arn:aws:bedrock-agentcore:{spec['bedrock_region']}:{spec['account_id']}:runtime/{runtime_id}",
                }
            if stage.endswith("-endpoint"):
                suffix = stage[:-9]
                runtime_id = name(spec, suffix, True).replace("-", "_") + "-1234567890"
                arn = f"arn:aws:bedrock-agentcore:{spec['bedrock_region']}:{spec['account_id']}:runtime/{runtime_id}"
                endpoint = "release_" + spec["release_id"].replace("-", "_")
                return {
                    "RuntimeArn": arn,
                    "RuntimeVersion": "1",
                    "EndpointName": endpoint,
                    "EndpointArn": arn + "/runtime-endpoint/" + endpoint + "-1234567890",
                }
        if command == "cursor-version":
            return self.outputs["secret"]
        if command == "model-secret-version":
            if self.missing_model_secret:
                raise VerificationError("Model API key secret has no unique current version")
            return self.outputs["model_secret"]
        if command == "upload":
            from infra.stacks import templates

            kind = kw["artifact_kind"]
            functions = {
                "tools": ("fetch_logs", "fetch_metrics"),
                "pipeline": tuple(self.artifacts),
                "observation": ("observation_probe", "observation_canary", "observation_receipt"),
                "host": ("incident_investigate",),
            }[kind]
            artifacts = {
                f: {
                    "sha256": "b" * 64,
                    "bucket": templates.bucket_name(
                        spec, "tools" if kind in {"tools", "host"} else "monitor"
                    ),
                    "key": f"releases/{spec['release_id']}/" + "b" * 64 + ".zip",
                    "version_id": "synthetic-version",
                }
                for f in functions
            }
            return artifacts["incident_investigate"] if kind == "host" else artifacts
        if command == "canary":
            self.canaries += 1
            self.canary_options = set(kw)
            return {
                "status": "PASS",
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "bundle_hash": bundle["review_hash"],
                "canary_tools": ["fetch_logs", "fetch_metrics"],
                "runtime_target": self.plan["runtime_config"]["runtime_target"],
                "runtime_release": owned_runtime.fingerprint(
                    spec, self.plan["runtime_config"], bundle["bindings"]
                ),
            }
        if command == "retirement-plan":
            return {"disable_alarms": [], "unsubscribe": []}
        if command == "verify-routing":
            try:
                return routing_health(
                    spec,
                    routing_clients(spec, self.subscriptions),
                    self.outputs["foundation"]["IngressQueueArn"],
                )
            except (
                PendingConfirmation
            ) as exc:  # The real op exits 3 and Driver.command raises the same Waiting.
                raise automation.confirmation_wait(exc.recipients) from None
        return {"status": "PASS"}

    def command(self, module, args, output=None):
        return {"status": "PASS"}


@pytest.mark.parametrize("target", ["standalone", "agentcore"])
@pytest.mark.parametrize("observations", [False, True])
def test_complete_orchestration_with_real_renderer_and_resume(tmp_path, monkeypatch, target, observations):
    p = configuration(tmp_path, target, observations)
    directory = tmp_path / "work"
    directory.mkdir()
    driver = FakeDriver(p, directory, monkeypatch)
    result = automation.deploy(p, directory, driver)
    assert result["status"] == "WAITING" and not result["auto_resume"]
    assert "--allow-model-invocation" in result["next"]
    assert driver.canaries == 0 and "routing" not in result["completed_stages"]
    result = automation.deploy(p, directory, driver, allow_model=True)
    assert result["status"] == "INFRASTRUCTURE_READY_MANUAL_ACCEPTANCE_PENDING"
    assert "routing" in result["completed_stages"] and driver.canaries == 1
    assert json.loads((directory / "ui-connection.json").read_text())["ui_role_arn"]
    result = automation.deploy(p, directory, driver, allow_model=True)
    assert result["status"] == "INFRASTRUCTURE_READY_MANUAL_ACCEPTANCE_PENDING" and driver.canaries == 1


def test_changed_plan_rejected_before_operations(tmp_path):
    p = configuration(tmp_path)
    directory = tmp_path / "work"
    directory.mkdir()
    automation.private_json(directory / "state.json", {"version": 1, "plan_hash": "changed"})
    with pytest.raises(VerificationError, match="Configuration/source changed"):
        automation.deploy(p, directory, Mock())


def test_paid_canary_ambiguity_never_retried(tmp_path, monkeypatch):
    p = configuration(tmp_path)
    directory = tmp_path / "work"
    directory.mkdir()
    driver = FakeDriver(p, directory, monkeypatch)
    automation.deploy(p, directory, driver)
    state = json.loads((directory / "state.json").read_text())
    state["canary_requested"] = True
    automation.private_json(directory / "state.json", state)
    result = automation.deploy(p, directory, driver, allow_model=True)
    assert result["status"] == "WAITING" and "ambiguous" in result["next"] and driver.canaries == 0
    result = automation.deploy(p, directory, driver, allow_model=True, retry_canary=True)
    assert result["status"] == "INFRASTRUCTURE_READY_MANUAL_ACCEPTANCE_PENDING" and driver.canaries == 1


@pytest.mark.parametrize(
    "decision,context", [("implicitDeny", []), ("explicitDeny", []), ("allowed", ["missing-condition"])]
)
def test_permission_denials_and_missing_context_are_not_success(decision, context):
    iam = Mock()
    iam.simulate_principal_policy.return_value = {
        "EvaluationResults": [
            {"EvalActionName": "iam:PassRole", "EvalDecision": decision, "MissingContextValues": context}
        ]
    }
    result = deployment_preflight.simulate(
        iam,
        {
            "principal": "arn:aws:iam::123456789012:role/operator",
            "actions": ["iam:PassRole"],
            "resource": "arn:aws:iam::123456789012:role/deploy",
            "region": "eu-central-1",
            "context": {"iam:PassedToService": "cloudformation.amazonaws.com"},
        },
    )
    assert result[0]["decision"] == decision and result[0]["missing_context"] == context


def test_permission_pagination_and_omitted_result():
    iam = Mock()
    iam.simulate_principal_policy.side_effect = [
        {"EvaluationResults": [], "IsTruncated": True, "Marker": "next"},
        {"EvaluationResults": [{"EvalActionName": "a", "EvalDecision": "allowed"}]},
    ]
    request = {"principal": "p", "region": "r", "resource": "*", "actions": ["a"], "context": {}}
    assert deployment_preflight.simulate(iam, request)[0]["decision"] == "allowed"
    assert iam.simulate_principal_policy.call_args.kwargs["Marker"] == "next"
    iam.simulate_principal_policy.side_effect = None
    iam.simulate_principal_policy.return_value = {"EvaluationResults": []}
    with pytest.raises(VerificationError, match="omitted"):
        deployment_preflight.simulate(iam, request)


def test_synthetic_preflight_no_clients(tmp_path):
    p = configuration(tmp_path)
    client = Mock(side_effect=AssertionError("AWS forbidden"))
    with pytest.raises(VerificationError, match="Synthetic"):
        deployment_preflight.check(p, client)
    client.assert_not_called()


def test_wrong_account_no_iam_or_write(tmp_path):
    p = configuration(tmp_path)
    p["spec"]["reference_only"] = False
    sts = Mock()
    sts.get_caller_identity.return_value = {"Account": "999999999999"}
    factory = Mock(return_value=sts)
    with pytest.raises(VerificationError, match="Wrong AWS"):
        deployment_preflight.check(p, factory)
    assert factory.call_count == 1


def test_existing_stack_access_denial_not_absence(tmp_path):
    p = configuration(tmp_path)
    driver = object.__new__(automation.Driver)
    driver.plan = p
    cfn = Mock()
    cfn.describe_stacks.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "private"}}, "DescribeStacks"
    )
    driver.clients = Mock(return_value=cfn)
    bundle = {"stages": {"routing": {"stack": "example", "region": "eu-central-1"}}, "spec": p["spec"]}
    with pytest.raises(ClientError):
        driver.inspect_stack(bundle, "routing")


@pytest.mark.parametrize("status", ["CREATE_IN_PROGRESS", "ROLLBACK_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"])
def test_in_progress_and_rollback_not_success(tmp_path, status):
    p = configuration(tmp_path)
    driver = object.__new__(automation.Driver)
    driver.clients = Mock()
    driver.inspect_stack = Mock(return_value={"StackStatus": status})
    driver.template_matches = Mock(return_value=False)
    driver.op = Mock()
    bundle = {
        "stages": {
            "owned-tools": {
                "stack": "example",
                "region": "eu-central-1",
                "create_only": True,
                "template_hash": "hash",
            }
        },
        "spec": p["spec"],
    }
    error = automation.Waiting if status == "CREATE_IN_PROGRESS" else VerificationError
    with pytest.raises(error):
        driver.stage(bundle, "owned-tools", {}, Mock())
    driver.op.assert_not_called()


def test_review_in_progress_continues_exact_change_set(tmp_path):
    p = configuration(tmp_path)
    driver = object.__new__(automation.Driver)
    driver.directory = tmp_path
    cfn = Mock()
    cfn.describe_change_set.return_value = {"Status": "CREATE_COMPLETE", "ExecutionStatus": "AVAILABLE"}
    driver.clients = Mock(return_value=cfn)
    driver.inspect_stack = Mock(return_value={"StackStatus": "REVIEW_IN_PROGRESS"})
    driver.op = Mock(
        return_value={
            "changes": [{"Type": "Resource", "ResourceChange": {"Action": "Add"}}],
            "change_set": "cs",
            "review_hash": "diff",
        }
    )
    bundle = {
        "review_hash": "a" * 64,
        "stages": {
            "owned-tools": {
                "stack": "example",
                "region": "eu-central-1",
                "create_only": True,
                "template_hash": "hash",
            }
        },
        "spec": p["spec"],
    }
    record = {}
    with pytest.raises(automation.Waiting) as exc:
        driver.stage(bundle, "owned-tools", record, Mock())
    assert exc.value.automatic and record["status"] == "EXECUTION_INTENT"
    assert [c.args[1] for c in driver.op.call_args_list] == ["inspect", "execute"]


@pytest.mark.parametrize(
    "action,replacement", [("Remove", "False"), ("Modify", "True"), ("Modify", "Conditional")]
)
def test_destructive_change_sets_never_execute(tmp_path, action, replacement):
    p = configuration(tmp_path)
    driver = object.__new__(automation.Driver)
    driver.directory = tmp_path
    cfn = Mock()
    cfn.describe_change_set.return_value = {"Status": "CREATE_COMPLETE", "ExecutionStatus": "AVAILABLE"}
    driver.clients = Mock(return_value=cfn)
    driver.inspect_stack = Mock(return_value={"StackStatus": "UPDATE_COMPLETE"})
    driver.template_matches = Mock(return_value=False)
    driver.op = Mock(
        return_value={
            "changes": [
                {"Type": "Resource", "ResourceChange": {"Action": action, "Replacement": replacement}}
            ]
        }
    )
    bundle = {
        "review_hash": "a" * 64,
        "stages": {
            "routing": {
                "stack": "example",
                "region": "eu-central-1",
                "create_only": False,
                "template_hash": "hash",
            }
        },
        "spec": p["spec"],
    }
    with pytest.raises(VerificationError, match="deletes/replaces"):
        driver.stage(bundle, "routing", {}, Mock())
    assert driver.op.call_count == 1


def test_profile_does_not_inherit_static_credentials(tmp_path, monkeypatch):
    planned = configuration(tmp_path)
    planned["profile"] = "customer-sso"
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "dummy")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "dummy")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "dummy")
    monkeypatch.setattr(automation, "factory", Mock())
    driver = automation.Driver(planned, tmp_path)
    assert driver.env["AWS_PROFILE"] == "customer-sso"
    assert not {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"} & driver.env.keys()
    assert os.environ["AWS_ACCESS_KEY_ID"] == "dummy"


def preflight_factory(planned, decision="allowed"):
    spec = planned["spec"]
    sts = Mock()
    sts.get_caller_identity.return_value = {
        "Account": spec["account_id"],
        "Arn": f"arn:aws:sts::{spec['account_id']}:assumed-role/customer-ci/synthetic-session",
    }
    iam = Mock()

    def role(**kw):
        return {
            "Role": {
                "RoleName": kw["RoleName"],
                "Arn": f"arn:aws:iam::{spec['account_id']}:role/{kw['RoleName']}",
                "AssumeRolePolicyDocument": {
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "cloudformation.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ]
                },
            }
        }

    iam.get_role.side_effect = role
    iam.simulate_principal_policy.side_effect = lambda **kw: {
        "EvaluationResults": [
            {"EvalActionName": a, "EvalDecision": decision, "MissingContextValues": []}
            for a in kw["ActionNames"]
        ]
    }
    ec2 = Mock()
    ec2.describe_regions.return_value = {
        "Regions": [{"RegionName": spec["monitor_region"], "OptInStatus": "opt-in-not-required"}]
    }
    ec2.describe_instances.return_value = {
        "Reservations": [
            {"Instances": [{"InstanceId": i["id"], "State": {"Name": "running"}} for i in spec["instances"]]}
        ]
    }
    lamb = Mock()
    lamb.get_account_settings.return_value = {"AccountLimit": {"UnreservedConcurrentExecutions": 110}}
    bedrock = Mock()
    bedrock.get_foundation_model.return_value = {
        "modelDetails": {"modelArn": spec.get("model_arns", ["unused-for-model-api"])[0]}
    }
    return {"sts": sts, "iam": iam, "ec2": ec2, "lambda": lamb, "bedrock": bedrock}


@pytest.mark.parametrize("decision", ["allowed", "implicitDeny", "explicitDeny"])
def test_complete_account_roles_inventory_capacity_permissions_preflight(tmp_path, decision):
    planned = configuration(tmp_path)
    planned["spec"]["reference_only"] = False
    clients = preflight_factory(planned, decision)
    result = deployment_preflight.check(planned, lambda service, region: clients[service])
    assert result["status"] == ("SIMULATED" if decision == "allowed" else "BLOCKED")
    assert bool(result["blockers"]) == (decision != "allowed")
    calls = clients["iam"].simulate_principal_policy.call_args_list
    assert calls and all(
        c.kwargs["PolicySourceArn"]
        in {planned["spec"]["ci_principal_arn"], planned["spec"]["deployment_role_arn"]}
        for c in calls
    )
    clients["bedrock"].get_foundation_model.assert_called_once()


@pytest.mark.parametrize("failure", ["wrong_role", "quota", "missing_ec2", "region_disabled", "wrong_trust"])
def test_account_prerequisites_fail_before_simulated_or_real_writes(tmp_path, failure):
    planned = configuration(tmp_path)
    planned["spec"]["reference_only"] = False
    clients = preflight_factory(planned)
    if failure == "wrong_role":
        clients["sts"].get_caller_identity.return_value["Arn"] = (
            "arn:aws:sts::123456789012:assumed-role/unrelated/session"
        )
    if failure == "quota":
        clients["lambda"].get_account_settings.return_value["AccountLimit"][
            "UnreservedConcurrentExecutions"
        ] = 10
    if failure == "missing_ec2":
        clients["ec2"].describe_instances.return_value = {"Reservations": []}
    if failure == "region_disabled":
        clients["ec2"].describe_regions.return_value = {"Regions": []}
    if failure == "wrong_trust":
        clients["iam"].get_role.side_effect = lambda **kw: {
            "Role": {
                "RoleName": kw["RoleName"],
                "Arn": f"arn:aws:iam::123456789012:role/{kw['RoleName']}",
                "AssumeRolePolicyDocument": {"Statement": []},
            }
        }
    with pytest.raises(VerificationError):
        deployment_preflight.check(planned, lambda service, region: clients[service])
    clients["iam"].simulate_principal_policy.assert_not_called()


def test_cli_synthetic_check_rejected_without_session(tmp_path, monkeypatch):
    monkeypatch.setattr(automation, "private_dir", lambda path: path)
    directory = tmp_path / ".local/customer"
    directory.mkdir(parents=True)
    configuration(directory)
    factory = Mock(side_effect=AssertionError("AWS forbidden"))
    monkeypatch.setattr(automation, "factory", factory)
    monkeypatch.setattr(
        "sys.argv",
        ["automation", "check", "--config", str(directory / "automation.json"), "--work-dir", str(directory)],
    )
    assert automation.main() == 1
    factory.assert_not_called()


def test_generated_ui_launch_uses_scoped_role_not_deployer(tmp_path, monkeypatch):
    from scripts.run_customer_ui import environment

    path = tmp_path / "ui.json"
    automation.private_json(
        path,
        {
            "environment": {
                "EXPECTED_ACCOUNT_ID": "123456789012",
                "MONITOR_REGION": "eu-central-1",
                "ENVIRONMENT": "staging",
            },
            "ui_role_arn": "arn:aws:iam::123456789012:role/tracaus/staging/ui",
        },
    )
    session = Mock()
    session.return_value.client.return_value.get_caller_identity.return_value = {
        "Account": "123456789012",
        "Arn": "arn:aws:sts::123456789012:assumed-role/ui/private-session",
    }
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "dummy")
    result = environment(path, "customer-ui", session_factory=session)
    assert result["AWS_PROFILE"] == "customer-ui" and result["PYTHON_DOTENV_DISABLED"] == "1"
    assert "AWS_SECRET_ACCESS_KEY" not in result
    session.return_value.client.return_value.get_caller_identity.return_value["Arn"] = (
        "arn:aws:sts::123456789012:assumed-role/customer-ci/private-session"
    )
    with pytest.raises(ValueError, match="UI profile"):
        environment(path, "customer-ci", session_factory=session)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        environment(path, "customer-ui", session_factory=session)


@pytest.mark.parametrize(
    "interrupted",
    [
        "owned-tools",
        "agentcore-runtime",
        "agentcore-endpoint",
        "durable-runtime",
        "observation-runtime",
        "routing",
    ],
)
def test_resume_at_interrupted_stage_reconciles_real_bindings(tmp_path, monkeypatch, interrupted):
    planned = configuration(tmp_path, "agentcore", True)
    directory = tmp_path / "work"
    directory.mkdir()
    driver = FakeDriver(planned, directory, monkeypatch)
    original = driver.stage
    hit = False

    def once(bundle, stage, record, save, **kwargs):
        nonlocal hit
        if stage == interrupted and not hit:
            hit = True
            record.update(status="INTENT", template_hash=bundle["stages"][stage]["template_hash"])
            save()
            raise automation.Waiting("Injected asynchronous AWS boundary", automatic=True)
        return original(bundle, stage, record, save, **kwargs)

    driver.stage = once
    for _ in range(3):
        result = automation.deploy(planned, directory, driver, allow_model=True)
        if result["status"] != "WAITING":
            break
    assert hit and result["status"] == "INFRASTRUCTURE_READY_MANUAL_ACCEPTANCE_PENDING"
    assert driver.canaries == 1


def test_production_candidate_does_not_bypass_staging_gate(tmp_path, monkeypatch):
    planned = configuration(tmp_path, environment="production")
    directory = tmp_path / "work"
    directory.mkdir()
    driver = FakeDriver(planned, directory, monkeypatch)
    driver.outputs["secret"]["arn"] = (
        "arn:aws:secretsmanager:eu-central-1:123456789012:secret:tracaus-production/log-cursor-123abc"
    )
    result = automation.deploy(planned, directory, driver, allow_model=True)
    assert result["status"] == "WAITING" and "Production candidates provisioned" in result["next"]
    assert driver.canaries == 0 and "routing" not in result["completed_stages"]


def test_mid_run_configuration_change_rejected_before_render(tmp_path, monkeypatch):
    planned = configuration(tmp_path)
    driver = FakeDriver(planned, tmp_path / "work", monkeypatch)
    path = tmp_path / "runtime.json"
    config = json.loads(path.read_text())
    config["retention_days"] += 1
    path.write_text(json.dumps(config))
    with pytest.raises(VerificationError, match="changed during deployment"):
        driver.render({})


def test_reservation_reconciliation_waits_for_complete_stack(tmp_path, monkeypatch):
    planned = configuration(tmp_path)
    directory = tmp_path / "work"
    directory.mkdir()
    driver = FakeDriver(planned, directory, monkeypatch)
    automation.deploy(planned, directory, driver)
    driver.inspect_stack = Mock(return_value={"StackStatus": "CREATE_IN_PROGRESS"})
    driver.template_matches = Mock(side_effect=AssertionError("No completed stack template yet"))
    result = automation.deploy(planned, directory, driver)
    assert result["status"] == "WAITING"
    driver.clients.assert_not_called()
    driver.template_matches.assert_not_called()


def test_operator_wrapper_binds_one_explicit_sdk_session(monkeypatch):
    from infra.deploy import operator

    setup = Mock()
    run = Mock()
    monkeypatch.setattr(operator.boto3, "setup_default_session", setup)
    monkeypatch.setattr(operator.runpy, "run_module", run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "operator",
            "--profile",
            "customer-sso",
            "--module",
            "infra.deploy.durable_ops",
            "--",
            "collect",
            "--bundle",
            ".local/customer/bundle",
        ],
    )
    operator.main()
    setup.assert_called_once_with(profile_name="customer-sso")
    run.assert_called_once_with("infra.deploy.durable_ops", run_name="__main__")
    assert __import__("sys").argv == [
        "infra.deploy.durable_ops",
        "collect",
        "--bundle",
        ".local/customer/bundle",
    ]
    assert "infra.identity_ops" not in operator.MODULES


# The stage lists of a plan, pinned as literals.
DEFAULT_STAGES = {
    ("standalone", False): [
        "foundation-tools",
        "foundation-monitor",
        "durable-foundation",
        "owned-tools",
        "durable-runtime",
        "candidate-verification",
        "staging-canary",
        "routing",
        "registration-verification",
        "manual-acceptance",
    ],
    ("standalone", True): [
        "foundation-tools",
        "foundation-monitor",
        "durable-foundation",
        "owned-tools",
        "durable-runtime",
        "observation-foundation",
        "observation-runtime",
        "health-bootstrap",
        "candidate-verification",
        "staging-canary",
        "routing",
        "observations",
        "registration-verification",
        "manual-acceptance",
    ],
    ("agentcore", False): [
        "foundation-tools",
        "foundation-monitor",
        "durable-foundation",
        "owned-tools",
        "agentcore-runtime",
        "agentcore-endpoint",
        "durable-runtime",
        "candidate-verification",
        "staging-canary",
        "routing",
        "registration-verification",
        "manual-acceptance",
    ],
    ("agentcore", True): [
        "foundation-tools",
        "foundation-monitor",
        "durable-foundation",
        "owned-tools",
        "agentcore-runtime",
        "agentcore-endpoint",
        "durable-runtime",
        "observation-foundation",
        "observation-runtime",
        "health-bootstrap",
        "candidate-verification",
        "staging-canary",
        "routing",
        "observations",
        "registration-verification",
        "manual-acceptance",
    ],
}


@pytest.mark.parametrize("target,observations", DEFAULT_STAGES)
def test_default_plan_stage_lists_are_pinned(tmp_path, target, observations):
    planned = configuration(tmp_path, target, observations)
    assert planned["stages"] == DEFAULT_STAGES[target, observations]
    assert "initial_access" not in planned
    assert set(planned["bootstrap_templates"]) == {
        "foundation-tools",
        "foundation-monitor",
        "durable-foundation",
    }
    assert not any(stage.startswith(("identity", "chat", "agentcore-chat")) for stage in planned["resources"])
    assert (
        "Local UI: export APP_PASSWORD (12+ characters) in your shell before launching it"
        in planned["manual"]
    )
    assert not any("OIDC" in item for item in planned["manual"])


def test_permission_screen_has_no_cloudtrail_or_grant_checks(tmp_path):
    actions = {a for request in configuration(tmp_path)["permission_screen"] for a in request["actions"]}
    assert not {a for a in actions if a.startswith("cloudtrail:")} and "dynamodb:PutItem" not in actions


@pytest.mark.parametrize("target", ["standalone", "agentcore"])
@pytest.mark.parametrize("observations", [False, True])
def test_default_deploy_reaches_the_canary_without_a_ticket(tmp_path, monkeypatch, target, observations):
    planned = configuration(tmp_path, target, observations)
    directory = tmp_path / "work"
    directory.mkdir()
    driver = FakeDriver(planned, directory, monkeypatch)
    result = automation.deploy(planned, directory, driver)
    assert result["status"] == "WAITING" and not result["auto_resume"]
    assert result["next"] == "Candidate ready: resume with --allow-model-invocation" and driver.canaries == 0
    result = automation.deploy(planned, directory, driver, allow_model=True)
    assert result["status"] == "INFRASTRUCTURE_READY_MANUAL_ACCEPTANCE_PENDING"
    assert driver.canaries == 1 and driver.canary_options == {"allow_model_invocation"}
    # Every CloudFormation stage of the plan completed, and nothing else.
    assert sorted(result["completed_stages"]) == sorted(
        s for s in planned["stages"] if s in planned["resources"]
    )
    state = json.loads((directory / "state.json").read_text())
    assert not {"identity", "model_secret"} & set(state["bindings"])
    assert not any("identity" in c or "chat" in c for c in driver.calls)
    connection = json.loads((directory / "ui-connection.json").read_text())
    assert set(connection) == {"environment", "ui_role_arn"}
    assert (
        automation.deploy(planned, directory, driver, allow_model=True)["status"]
        == "INFRASTRUCTURE_READY_MANUAL_ACCEPTANCE_PENDING"
        and driver.canaries == 1
    )


def test_default_mode_paid_canary_retry_needs_authorization_not_ticket(tmp_path, monkeypatch):
    planned = configuration(tmp_path)
    directory = tmp_path / "work"
    directory.mkdir()
    driver = FakeDriver(planned, directory, monkeypatch)
    automation.deploy(planned, directory, driver)
    state = json.loads((directory / "state.json").read_text())
    state["canary_requested"] = True
    automation.private_json(directory / "state.json", state)
    with pytest.raises(VerificationError, match="authorization"):
        automation.deploy(planned, directory, driver, retry_canary=True)
    result = automation.deploy(planned, directory, driver, allow_model=True)
    assert result["status"] == "WAITING" and "ambiguous" in result["next"] and driver.canaries == 0
    result = automation.deploy(planned, directory, driver, allow_model=True, retry_canary=True)
    assert result["status"] == "INFRASTRUCTURE_READY_MANUAL_ACCEPTANCE_PENDING" and driver.canaries == 1


def test_reserved_concurrency_counts_only_what_the_templates_reserve(tmp_path, monkeypatch):
    planned = configuration(tmp_path)
    initial = planned["runtime_config"]["initial_reserved_concurrency"]
    directory = tmp_path / "work"
    directory.mkdir()
    driver = FakeDriver(planned, directory, monkeypatch)
    seen = []

    def check(plan, clients, **kw):
        seen.append(kw["remaining_reserved"])
        return {"status": "SIMULATED"}

    monkeypatch.setattr(deployment_preflight, "check", check)
    driver.preflight = None
    automation.deploy(planned, directory, driver)
    assert seen == [initial]
    # Resume: the reservation the plan makes is verified live and leaves nothing to reserve.
    names = {driver.outputs["versions"]["InitialVersionArn"].rsplit(":", 1)[0]: initial}
    lambda_client = Mock()
    lambda_client.get_function_concurrency.side_effect = lambda FunctionName: (
        {"ReservedConcurrentExecutions": names[FunctionName]} if FunctionName in names else {}
    )
    driver.clients = Mock(return_value=lambda_client)
    driver.inspect_stack = Mock(return_value={"StackStatus": "CREATE_COMPLETE"})
    driver.template_matches = Mock(return_value=True)
    driver.preflight = None
    automation.deploy(planned, directory, driver)
    assert seen[-1] == 0


def test_preflight_capacity_follows_reservations_the_plan_makes(tmp_path):
    planned = configuration(tmp_path)
    planned["spec"]["reference_only"] = False
    clients = preflight_factory(planned)
    # 103 unreserved leaves 3 reservable: the initial reservation of 2 fits.
    clients["lambda"].get_account_settings.return_value["AccountLimit"]["UnreservedConcurrentExecutions"] = (
        103
    )
    result = deployment_preflight.check(planned, lambda service, region: clients[service])
    assert result["status"] == "SIMULATED"


# model_api plans insert exactly one read-only secret-version stage; Bedrock plans do not.
@pytest.mark.parametrize("target", ["standalone", "agentcore"])
def test_model_api_stage_order_pure(target):
    config = {"runtime_target": target}
    for spec in ({}, {"model_provider": "bedrock"}):
        assert "model-secret" not in automation.stage_order(spec, config)
    stages = automation.stage_order({"model_provider": "model_api"}, config)
    assert stages.count("model-secret") == 1
    assert stages.index("durable-foundation") < stages.index("model-secret") < stages.index("owned-tools")
    assert [s for s in stages if s != "model-secret"] == automation.stage_order({}, config)


def test_model_api_plan_and_resumable_secret_binding(tmp_path, monkeypatch):
    planned = configuration(tmp_path, model_api=True)
    assert planned["stages"].count("model-secret") == 1
    assert "model-secret" not in planned["resources"]
    directory = tmp_path / "work"
    directory.mkdir()
    driver = FakeDriver(planned, directory, monkeypatch)
    automation.deploy(planned, directory, driver)
    result = automation.deploy(planned, directory, driver, allow_model=True)
    assert result["status"] == "INFRASTRUCTURE_READY_MANUAL_ACCEPTANCE_PENDING" and driver.canaries == 1
    state = json.loads((directory / "state.json").read_text())
    assert state["bindings"]["model_secret"] == driver.outputs["model_secret"]
    assert driver.calls.count("model-secret-version") == 1  # pinned once, never re-resolved on resume
    assert driver.calls.index("collect:durable-foundation") < driver.calls.index("model-secret-version")
    assert driver.calls.index("model-secret-version") < driver.calls.index("owned-tools")


def test_bedrock_deploy_never_resolves_a_model_secret(tmp_path, monkeypatch):
    planned = configuration(tmp_path)
    directory = tmp_path / "work"
    directory.mkdir()
    driver = FakeDriver(planned, directory, monkeypatch)
    automation.deploy(planned, directory, driver)
    assert "model-secret-version" not in driver.calls
    assert "model_secret" not in json.loads((directory / "state.json").read_text())["bindings"]


def test_missing_model_secret_fails_closed_before_any_runtime_or_model_call(tmp_path, monkeypatch):
    planned = configuration(tmp_path, model_api=True)
    directory = tmp_path / "work"
    directory.mkdir()
    driver = FakeDriver(planned, directory, monkeypatch)
    driver.missing_model_secret = True
    with pytest.raises(VerificationError, match="Model API key secret"):
        automation.deploy(planned, directory, driver, allow_model=True)
    assert driver.calls[-1] == "model-secret-version" and driver.canaries == 0
    assert not {"owned-tools", "durable-runtime", "upload", "canary"} & set(driver.calls)
    state = json.loads((directory / "state.json").read_text())
    assert state["status"] == "FAILED" and "model_secret" not in state["bindings"]


def model_api_preflight(tmp_path, secret):
    planned = configuration(tmp_path)
    planned["spec"]["reference_only"] = False
    planned["spec"].pop("model_arns")
    planned["spec"]["model_provider"] = "model_api"
    clients = preflight_factory(planned)
    clients["secretsmanager"] = Mock()
    if isinstance(secret, Exception):
        clients["secretsmanager"].describe_secret.side_effect = secret
    else:
        clients["secretsmanager"].describe_secret.return_value = secret
    return planned, clients


def test_model_api_preflight_checks_secret_by_name_not_catalog_or_value(tmp_path):
    secret = {"VersionIdsToStages": {"v1": ["AWSPREVIOUS"], "v2": ["AWSCURRENT"]}}
    planned, clients = model_api_preflight(tmp_path, secret)
    clients["bedrock"] = Mock(side_effect=AssertionError("Bedrock catalog does not apply"))
    result = deployment_preflight.check(planned, lambda service, region: clients[service])
    assert result["status"] == "SIMULATED"
    assert result["model_catalog_visible"] is None and result["model_secret_present"] is True
    assert any("not read" in item for item in result["limitations"])
    spec = planned["spec"]
    clients["secretsmanager"].describe_secret.assert_called_once_with(
        SecretId=prefix(spec) + "/model-api-key"
    )
    assert not clients["secretsmanager"].get_secret_value.called


@pytest.mark.parametrize("failure", ["absent", "no_current", "no_versions", "two_current", "denied"])
def test_model_api_preflight_secret_failures_block_before_simulation(tmp_path, failure):
    error = {
        "absent": ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "DescribeSecret"),
        "denied": ClientError({"Error": {"Code": "AccessDeniedException"}}, "DescribeSecret"),
        "no_current": {"VersionIdsToStages": {"v1": ["AWSPREVIOUS"]}},
        "no_versions": {},
        "two_current": {"VersionIdsToStages": {"v1": ["AWSCURRENT"], "v2": ["AWSCURRENT"]}},
    }[failure]
    planned, clients = model_api_preflight(tmp_path, error)
    expected = ClientError if failure == "denied" else VerificationError
    with pytest.raises(expected):
        deployment_preflight.check(planned, lambda service, region: clients[service])
    clients["iam"].simulate_principal_policy.assert_not_called()


def test_bedrock_preflight_report_keys_unchanged(tmp_path):
    planned = configuration(tmp_path)
    planned["spec"]["reference_only"] = False
    clients = preflight_factory(planned)
    result = deployment_preflight.check(planned, lambda service, region: clients[service])
    assert result["model_catalog_visible"] is True and "model_secret_present" not in result
    assert not any("secret" in item for item in result["limitations"])


def run_main(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["automation", *map(str, argv)])
    return automation.main()


def test_init_copies_the_default_runtime_and_plans(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(automation, "private_dir", lambda path: path)
    assert run_main(monkeypatch, "init", "--work-dir", tmp_path) == 0
    assert (tmp_path / "runtime.json").read_bytes() == (ROOT / "examples/durable.example.json").read_bytes()
    assert (tmp_path / "runtime.json").stat().st_mode & 0o777 == 0o600
    assert "initial_access" not in json.loads((tmp_path / "automation.json").read_text())
    assert "docs/DEPLOY.md" in capsys.readouterr().out
    assert (
        run_main(monkeypatch, "dry-run", "--config", tmp_path / "automation.json", "--work-dir", tmp_path)
        == 0
    )
    stages = json.loads(capsys.readouterr().out)["stages"]
    assert stages == DEFAULT_STAGES["standalone", False]
    # Init never overwrites what the customer already filled in.
    assert run_main(monkeypatch, "init", "--work-dir", tmp_path) == 1


def test_removed_identity_options_are_no_longer_accepted(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(automation, "private_dir", lambda path: path)
    for argv in (["init", "--identity"], ["apply", "--access-ticket-file", tmp_path / "ticket"]):
        with pytest.raises(SystemExit) as stop:
            run_main(monkeypatch, *argv, "--work-dir", tmp_path)
        assert stop.value.code == 2 and "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize(
    "flags,accepted",
    [
        (["--allow-model-invocation"], True),
        (["--allow-model-invocation", "--retry-canary"], True),
        (["--retry-canary"], False),
    ],
)
def test_apply_paid_canary_flags_need_explicit_authorization(tmp_path, monkeypatch, capsys, flags, accepted):
    monkeypatch.setattr(automation, "private_dir", lambda path: path)
    planned = configuration(tmp_path)
    planned["spec"]["reference_only"] = False
    monkeypatch.setattr(automation, "plan", lambda path: planned)
    monkeypatch.setattr(automation.subprocess, "check_output", lambda *a, **kw: "")
    driver = Mock()
    monkeypatch.setattr(automation, "Driver", driver)
    deploy = Mock(
        return_value={"status": "INFRASTRUCTURE_READY_MANUAL_ACCEPTANCE_PENDING", "auto_resume": False}
    )
    monkeypatch.setattr(automation, "deploy", deploy)
    argv = ["apply", "--config", tmp_path / "automation.json", "--work-dir", tmp_path]
    argv += ["--plan-hash", planned["plan_hash"], *flags]
    assert run_main(monkeypatch, *argv) == (0 if accepted else 1)
    assert deploy.called is accepted and driver.called is accepted
    if accepted:
        assert deploy.call_args.kwargs == {
            "allow_model": True,
            "retry_canary": "--retry-canary" in flags,
        }
    else:
        assert "--retry-canary needs --allow-model-invocation" in capsys.readouterr().err


def launcher_connection(tmp_path, **extra):
    from scripts.run_customer_ui import environment

    path = tmp_path / "ui.json"
    automation.private_json(
        path,
        {
            "environment": {
                "EXPECTED_ACCOUNT_ID": "123456789012",
                "MONITOR_REGION": "eu-central-1",
                "ENVIRONMENT": "staging",
                **extra,
            },
            "ui_role_arn": "arn:aws:iam::123456789012:role/tracaus/staging/ui",
        },
    )
    session = Mock()
    session.return_value.client.return_value.get_caller_identity.return_value = {
        "Account": "123456789012",
        "Arn": "arn:aws:sts::123456789012:assumed-role/ui/private-session",
    }
    return lambda: environment(path, "customer-ui", session_factory=session)


def test_launcher_keeps_the_operator_password_and_passes_connection_values_through(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "x" * 16)
    model = json.dumps({"protocol": "openai", "base_url": "https://models.example.invalid/v1"})
    result = launcher_connection(tmp_path, MODEL_API=model)()
    assert "APP_PASSWORD" in result
    assert result["MODEL_API"] == model  # connection values pass straight through, no allowlist


@pytest.mark.parametrize("password,hint", [(False, True), (True, False)])
def test_launcher_hints_when_default_mode_has_no_password(tmp_path, monkeypatch, capsys, password, hint):
    from scripts import run_customer_ui

    monkeypatch.delenv("APP_PASSWORD", raising=False)
    if password:
        monkeypatch.setenv("APP_PASSWORD", "x" * 16)
    build = launcher_connection(tmp_path)
    monkeypatch.setattr(run_customer_ui, "environment", lambda path, profile: build())
    execve = Mock()
    monkeypatch.setattr(run_customer_ui.os, "execve", execve)
    monkeypatch.setattr(run_customer_ui.os, "chdir", Mock())
    monkeypatch.setattr(
        "sys.argv", ["run_customer_ui", "--connection", str(tmp_path / "ui.json"), "--profile", "customer-ui"]
    )
    run_customer_ui.main()
    execve.assert_called_once()
    assert ("APP_PASSWORD" in capsys.readouterr().err) is hint


def launch(tmp_path, monkeypatch, *argv):
    from scripts import run_customer_ui

    build = launcher_connection(tmp_path)
    monkeypatch.setattr(run_customer_ui, "environment", lambda path, profile: build())
    execve = Mock()
    monkeypatch.setattr(run_customer_ui.os, "execve", execve)
    monkeypatch.setattr(run_customer_ui.os, "chdir", Mock())
    monkeypatch.setattr(
        "sys.argv",
        ["run_customer_ui", "--connection", str(tmp_path / "ui.json"), "--profile", "customer-ui", *argv],
    )
    return run_customer_ui.main(), execve


def test_launcher_team_file_turns_on_team_mode_and_drops_the_password(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "x" * 16)
    team_path = tmp_path / "team.toml"
    team_path.write_text('issuer = "https://login.example.invalid"\n')
    team_path.chmod(0o600)
    code, execve = launch(tmp_path, monkeypatch, "--team-file", str(team_path))
    assert code == 0
    env = execve.call_args.args[2]
    assert env["TRACAUS_TEAM_FILE"] == str(team_path.resolve())
    assert "APP_PASSWORD" not in env


def test_launcher_refuses_a_team_file_others_can_write(tmp_path, monkeypatch, capsys):
    team_path = tmp_path / "team.toml"
    team_path.write_text("x = 1\n")
    team_path.chmod(0o664)
    code, execve = launch(tmp_path, monkeypatch, "--team-file", str(team_path))
    assert code == 1
    execve.assert_not_called()
    assert "team file" in capsys.readouterr().err.lower()


def test_launcher_ignores_a_stray_team_file_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACAUS_TEAM_FILE", str(tmp_path / "stray.toml"))
    result = launcher_connection(tmp_path)()
    assert "TRACAUS_TEAM_FILE" not in result


def run_model_secret_op(tmp_path, monkeypatch, secret, *, provider="model_api"):
    from infra.deploy import durable_ops

    planned = configuration(tmp_path, model_api=provider == "model_api")
    spec = {**planned["spec"], "reference_only": False}
    secretsmanager = Mock()
    secretsmanager.describe_secret.return_value = secret(spec) if callable(secret) else secret
    sts_and_sm = Mock(side_effect=lambda service, region: secretsmanager)
    monkeypatch.setattr(durable_ops, "read_bundle", lambda path, review_hash: {"spec": spec})
    monkeypatch.setattr(durable_ops, "assert_account", Mock())
    monkeypatch.setattr(durable_ops, "clients", sts_and_sm)
    output = tmp_path / "out.json"
    monkeypatch.setattr(
        "sys.argv",
        ["durable_ops", "model-secret-version", "--bundle", str(tmp_path), "--review-hash", "h"]
        + ["--output", str(output)],
    )
    return durable_ops.main(), output, secretsmanager, spec


def model_secret(spec, versions=None, **extra):
    return {
        "Name": prefix(spec) + "/model-api-key",
        "ARN": f"arn:aws:secretsmanager:{spec['bedrock_region']}:{spec['account_id']}:secret:{prefix(spec)}/model-api-key-123abc",
        "VersionIdsToStages": versions or {"a" * 32: ["AWSPREVIOUS"], "b" * 32: ["AWSCURRENT"]},
        **extra,
    }


def test_model_secret_version_op_pins_the_single_current_version_without_reading_the_value(
    tmp_path, monkeypatch
):
    rc, output, secretsmanager, spec = run_model_secret_op(tmp_path, monkeypatch, model_secret)
    assert rc == 0
    assert json.loads(output.read_text()) == {
        "arn": model_secret(spec)["ARN"],
        "version_id": "b" * 32,
    }
    secretsmanager.describe_secret.assert_called_once_with(SecretId=prefix(spec) + "/model-api-key")
    assert not secretsmanager.get_secret_value.called


@pytest.mark.parametrize(
    "mutation",
    [
        lambda spec: model_secret(spec, {"a" * 32: ["AWSPREVIOUS"]}),
        lambda spec: model_secret(spec, {"a" * 32: ["AWSCURRENT"], "b" * 32: ["AWSCURRENT"]}),
        lambda spec: model_secret(spec, Name="tracaus-staging/other"),
    ],
    ids=["no_current", "two_current", "other_secret"],
)
def test_model_secret_version_op_rejects_ambiguous_or_foreign_secrets(tmp_path, monkeypatch, mutation):
    rc, output, secretsmanager, _ = run_model_secret_op(tmp_path, monkeypatch, mutation)
    assert rc == 1 and not output.exists() and secretsmanager.describe_secret.called


def test_model_secret_version_op_is_model_api_only(tmp_path, monkeypatch):
    rc, output, secretsmanager, _ = run_model_secret_op(
        tmp_path, monkeypatch, model_secret, provider="bedrock"
    )
    assert rc == 1 and not output.exists() and not secretsmanager.describe_secret.called


def test_model_api_requires_standalone_runtime(tmp_path):
    with pytest.raises(VerificationError, match="standalone"):
        configuration(tmp_path, "agentcore", model_api=True)


def primary_email(driver):
    return next(s for s in driver.subscriptions if s["Protocol"] == "email")


def awaiting_routing_verification(tmp_path, monkeypatch):
    """A deployment that has provisioned routing and will run its registration verification next."""
    planned = configuration(tmp_path)
    directory = tmp_path / "work"
    directory.mkdir()
    driver = FakeDriver(planned, directory, monkeypatch)
    return planned, directory, driver


def journal(directory):
    return json.loads((directory / "state.json").read_text())


def test_pending_email_confirmation_waits_instead_of_failing(tmp_path, monkeypatch):
    planned, directory, driver = awaiting_routing_verification(tmp_path, monkeypatch)
    primary_email(driver)["SubscriptionArn"] = "PendingConfirmation"
    result = automation.deploy(planned, directory, driver, allow_model=True)
    assert result["status"] == "WAITING" and not result["auto_resume"]
    assert "notification_email" in result["next"] and "same plan hash" in result["next"]
    assert planned["spec"]["notification_email"] not in result["next"]  # key names only, never the address
    state = journal(directory)
    assert state["status"] == "WAITING" and state["next"] == result["next"]
    assert state["stages"]["routing"]["status"] == "COMPLETE"
    assert "routing" in result["completed_stages"] and driver.canaries == 1
    # Nothing past verification ran, and nothing was recorded as verified.
    assert "collect:routing" not in driver.calls
    connection = directory / "ui-connection.json"
    assert not connection.exists() or "ui_role_arn" not in json.loads(connection.read_text())


def test_resume_after_confirmation_reruns_only_verification_and_finishes(tmp_path, monkeypatch):
    planned, directory, driver = awaiting_routing_verification(tmp_path, monkeypatch)
    primary_email(driver)["SubscriptionArn"] = "PendingConfirmation"
    automation.deploy(planned, directory, driver, allow_model=True)
    # Resuming before the click still waits; it is neither a failure nor progress.
    result = automation.deploy(planned, directory, driver, allow_model=True)
    assert result["status"] == "WAITING" and journal(directory)["status"] == "WAITING"
    primary_email(driver)["SubscriptionArn"] = driver.subscriptions[1]["SubscriptionArn"] + ":confirmed"
    before = len(driver.calls)
    result = automation.deploy(planned, directory, driver, allow_model=True)
    assert result["status"] == "INFRASTRUCTURE_READY_MANUAL_ACCEPTANCE_PENDING"
    assert "verify-routing" in driver.calls[before:] and "collect:routing" in driver.calls[before:]
    # No rebuild, upload, second paid canary or new change set: provisioning is not repeated.
    assert not {"build", "upload", "canary", "change-set", "execute"} & set(driver.calls[before:])
    assert driver.canaries == 1 and driver.calls.count("build") == 1
    assert json.loads((directory / "ui-connection.json").read_text())["ui_role_arn"]


def mismatch(kind, driver, spec):
    email, ingress = primary_email(driver), driver.subscriptions[1]
    pending = {"SubscriptionArn": "PendingConfirmation"}
    if kind == "wrong_endpoint":
        email["Endpoint"] = "someone-else@example.invalid"
    elif kind == "wrong_endpoint_pending":
        email.update(Endpoint="someone-else@example.invalid", **pending)
    elif kind == "missing":
        driver.subscriptions.remove(email)
    elif kind == "missing_ingress_while_pending":
        email.update(pending)
        driver.subscriptions.remove(ingress)
    elif kind == "wrong_protocol":
        email["Protocol"] = "https"
    elif kind == "wrong_protocol_pending":
        email.update(Protocol="https", **pending)
    elif kind == "unsubscribed":
        email["SubscriptionArn"] = "Deleted"
    elif kind == "wrong_topic":
        email["TopicArn"] = topic_arn(spec, "alarms")
    elif kind == "extra_subscriber_while_pending":
        email.update(pending)
        driver.subscriptions.append(
            {**email, "Endpoint": "extra@example.invalid", "SubscriptionArn": "arn:x:1"}
        )


@pytest.mark.parametrize(
    "kind",
    [
        "wrong_endpoint",
        "wrong_endpoint_pending",
        "missing",
        "missing_ingress_while_pending",
        "wrong_protocol",
        "wrong_protocol_pending",
        "unsubscribed",
        "wrong_topic",
        "extra_subscriber_while_pending",
    ],
)
def test_every_other_subscription_mismatch_still_fails_closed(tmp_path, monkeypatch, kind):
    planned, directory, driver = awaiting_routing_verification(tmp_path, monkeypatch)
    mismatch(kind, driver, planned["spec"])
    with pytest.raises(VerificationError) as failure:
        automation.deploy(planned, directory, driver, allow_model=True)
    assert not isinstance(failure.value, PendingConfirmation)
    assert journal(directory)["status"] == "FAILED" and "collect:routing" not in driver.calls


def test_routing_verification_waits_only_after_every_other_check_passed(tmp_path, monkeypatch):
    from collections import defaultdict

    from infra.deploy import durable_ops

    spec = {**configuration(tmp_path)["spec"], "reference_only": False}
    queues = {key: "arn:aws:sqs:eu-central-1:123456789012:q" for key in ("Ingress", "Dispatch", "Work")}
    outputs = {
        "IngressQueueArn": queues["Ingress"],
        "StreamArn": queues["Dispatch"],
        "WorkQueueArn": queues["Work"],
        "InitialQueueArn": queues["Work"],
        "ReportQueueArn": queues["Work"],
    }
    for name_, value in {
        "assert_account": Mock(),
        "owned_stack": Mock(),
        "verify_capture": Mock(return_value=outputs),
        "routing_health": Mock(side_effect=PendingConfirmation(["notification_email"])),
    }.items():
        monkeypatch.setattr(durable_ops, name_, value)
    monkeypatch.setattr(durable_ops, "verify_ui_role", Mock())
    monkeypatch.setattr(
        durable_ops.durable_templates,
        "active_routing",
        Mock(return_value={"Resources": {"Ec2Down": {"Properties": {"Targets": []}}}}),
    )
    drifted = Mock()  # A durable event mapping is missing while the subscription is also pending.
    drifted.get_paginator.return_value.paginate.return_value = [{"EventSourceMappings": []}]
    monkeypatch.setattr(durable_ops, "clients", lambda service, region: drifted)
    bundle = {
        "spec": spec,
        "config": {"investigation_paused": True},
        "bindings": {
            "versions": defaultdict(lambda: "arn:aws:lambda:eu-central-1:123456789012:function:f:1")
        },
    }
    with pytest.raises(VerificationError, match="event mapping") as failure:
        durable_ops.verify_routing(bundle)
    assert not isinstance(failure.value, PendingConfirmation)


def test_pending_exit_code_is_distinct_and_names_config_keys_only(tmp_path, monkeypatch):
    from infra.deploy import durable_ops

    output = tmp_path / "out.json"
    monkeypatch.setattr(
        durable_ops, "read_bundle", lambda path, review_hash: {"spec": {"reference_only": False}}
    )
    monkeypatch.setattr(
        "sys.argv",
        ["durable_ops", "verify-routing", "--bundle", str(tmp_path), "--review-hash", "h"]
        + ["--output", str(output)],
    )
    monkeypatch.setattr(
        durable_ops, "verify_routing", Mock(side_effect=PendingConfirmation(["notification_email"]))
    )
    assert durable_ops.main() == 3
    assert json.loads(output.read_text()) == {
        "status": "PENDING_CONFIRMATION",
        "recipients": ["notification_email"],
    }
    output.unlink()
    monkeypatch.setattr(durable_ops, "verify_routing", Mock(side_effect=VerificationError("drift")))
    assert durable_ops.main() == 1 and not output.exists()


def test_driver_turns_only_the_pending_exit_of_a_durable_operation_into_waiting(tmp_path, monkeypatch):
    planned = configuration(tmp_path)
    monkeypatch.setattr(automation, "factory", Mock())
    directory = tmp_path / "work"
    directory.mkdir()
    driver = automation.Driver(planned, directory)
    output = directory / "operation.json"

    def exits(code):
        def run(*args, **kwargs):
            output.write_text(
                json.dumps({"status": "PENDING", "recipients": ["notification_email", "fallback_email"]})
            )
            return Mock(returncode=code)

        monkeypatch.setattr(automation.subprocess, "run", run)

    exits(3)
    with pytest.raises(automation.Waiting, match="notification_email and fallback_email") as waiting:
        driver.command("infra.deploy.durable_ops", ["verify-routing"], output)
    assert not waiting.value.automatic and "same plan hash" in str(waiting.value)
    for module, code in (
        ("infra", 3),
        ("infra.deploy.durable_ops", 1),
        ("infra.deploy.durable_ops", 2),
    ):
        exits(code)
        with pytest.raises(VerificationError, match="failed"):
            driver.command(module, ["verify-routing"], output)


def owned_routing_resources(spec, summaries):
    from infra.maintain import reconcile

    cfn = Mock()
    cfn.describe_stacks.return_value = {
        "Stacks": [
            {
                "Tags": [
                    {"Key": "Project", "Value": spec["project"]},
                    {"Key": "Environment", "Value": spec["environment"]},
                    {"Key": "ManagedBy", "Value": "tracaus-cloudformation"},
                ]
            }
        ]
    }
    cfn.get_paginator.return_value.paginate.return_value = [{"StackResourceSummaries": summaries}]
    return reconcile.owned_resources(spec, lambda service, region: cfn)


def test_retirement_plan_accepts_only_the_current_recipients_pending_subscription(tmp_path):
    from infra.maintain import reconcile
    from infra.spec import digest

    spec = configuration(tmp_path)["spec"]
    pending = {
        "ResourceType": "AWS::SNS::Subscription",
        "ResourceStatus": "CREATE_COMPLETE",
        "PhysicalResourceId": "PendingConfirmation",
    }
    current = {**pending, "LogicalResourceId": "Email" + digest(spec["notification_email"])[:16]}
    plan = reconcile.plan(spec, owned_routing_resources(spec, [current]), "worker")
    assert plan["unsubscribe"] == [] and plan["disable_alarms"] == []
    # A pending subscription of any other (retired) recipient still needs an explicit decision.
    former = {**pending, "LogicalResourceId": "Email" + digest("former@example.invalid")[:16]}
    with pytest.raises(VerificationError, match="unconfirmed"):
        owned_routing_resources(spec, [former])
