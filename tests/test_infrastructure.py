import copy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from infra.deploy import durable, durable_ops, release
from infra.deploy.verify import (
    VerificationError,
    assert_account,
    assert_concurrency,
    assert_stack_absent,
    coverage,
    exact_metric_exists,
)
from infra.maintain import reconcile
from infra.spec import ROOT, alarm_descriptors, load, log_groups, metric_catalog, name, topic_arn
from infra.stacks import templates
from scripts.dev.validate_infrastructure import examples


@pytest.fixture
def spec():
    return {**load(ROOT / "examples/deployment.example.json"), "reference_only": False}


def test_same_and_split_region_ownership(spec):
    for region in ("eu-central-1", "us-east-1"):
        spec["bedrock_region"] = region
        rendered = examples(spec)
        for stage, template in rendered.items():
            expected = durable_ops.stage_region(spec, stage)
            assert template["Metadata"]["Tracaus"]["ExpectedRegion"] == expected
            assert template["Metadata"]["Tracaus"]["Environment"] == "staging"
        assert templates.bucket_name(spec, "tools") != templates.bucket_name(spec, "monitor")


def test_staging_and_production_have_disjoint_physical_names(spec):
    def names(env):
        target = {**spec, "environment": env}
        return {
            resource["Properties"][key]
            for t in examples(target).values()
            for resource in t["Resources"].values()
            for key in ("FunctionName", "BucketName", "TopicName", "AlarmName", "LogGroupName", "AgentName")
            if isinstance(resource["Properties"].get(key), str)
        }

    assert not names("staging") & names("production")


def test_new_candidate_never_references_previous_release(spec):
    before = examples(spec)
    after = examples({**spec, "release_id": "next002"})
    for stage in ("owned-tools",):
        assert spec["release_id"] not in json.dumps(after[stage])
        assert name(spec, stage, True) != name({**spec, "release_id": "next002"}, stage, True)
    tools = before["owned-tools"]["Resources"]
    assert not any(r["Type"].startswith("AWS::Bedrock::") for r in tools.values())
    assert not any(r["Type"] == "AWS::Lambda::Permission" for r in tools.values())


def test_retention_and_exact_artifact_versions(spec):
    for template in examples(spec).values():
        for resource in template["Resources"].values():
            if resource["Type"] in {
                "AWS::S3::Bucket",
                "AWS::SecretsManager::Secret",
                "AWS::Logs::LogGroup",
                "AWS::Lambda::Version",
                "AWS::Lambda::Function",
            }:
                assert resource["DeletionPolicy"] == resource["UpdateReplacePolicy"] == "Retain"
            if resource["Type"] == "AWS::Lambda::Function":
                assert resource["Properties"]["Code"]["S3ObjectVersion"]
            if resource["Type"] == "AWS::Lambda::Version":
                assert resource["Properties"]["CodeSha256"]


def test_roles_cannot_deploy_and_only_log_tool_reads_secret(spec):
    rendered = examples(spec)
    allowed_secret_roles = []
    for stage, template in rendered.items():
        for logical, resource in template["Resources"].items():
            if resource["Type"] != "AWS::IAM::Role":
                continue
            assert resource["Properties"]["Path"] == "/tracaus/staging/"
            for policy in resource["Properties"]["Policies"]:
                for statement in policy["PolicyDocument"]["Statement"]:
                    actions = statement["Action"]
                    actions = [actions] if isinstance(actions, str) else actions
                    assert not any(
                        action == "*"
                        or action.startswith(
                            (
                                "iam:",
                                "cloudformation:Create",
                                "cloudformation:Update",
                                "lambda:Update",
                                "lambda:Create",
                                "sts:AssumeRole",
                            )
                        )
                        for action in actions
                    )
                    if "secretsmanager:GetSecretValue" in actions:
                        allowed_secret_roles.append((stage, logical))
                        assert ":secret:tracaus-staging/log-cursor-" in statement["Resource"]
    assert allowed_secret_roles == [("owned-tools", "LogsRole")]
    log_policy = rendered["owned-tools"]["Resources"]["LogsRole"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"]
    query = next(s for s in log_policy if "logs:StartQuery" in s["Action"])
    assert all(":log-group:/tracaus/staging/i-0123456789abcdef0/" in arn for arn in query["Resource"])
    assert query["Condition"]["StringEquals"]["aws:RequestedRegion"] == spec["monitor_region"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s.update(unknown=True),
        lambda s: s.update(environment="prod"),
        lambda s: s.update(reserved_concurrency=0),
        lambda s: s.update(model_arns=["*"]),
        lambda s: s.update(executor_mode="release-name"),
        lambda s: s.update(ui_principal_arn=s["deployment_role_arn"]),
        lambda s: s["instances"].append(copy.deepcopy(s["instances"][0])),
    ],
)
def test_invalid_spec_fails_locally(spec, tmp_path, mutation):
    mutation(spec)
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    with pytest.raises(ValueError):
        load(path)


USERINFO = "fixture-user" + ":" + "fixture-pw"  # Built at runtime: not a committed credential.


def api_variant(spec, **api):
    value = {k: v for k, v in spec.items() if k != "model_arns"}
    value.update(
        model_provider="model_api",
        model_id="example-model-v1",
        model_api={"protocol": "openai", "base_url": "https://llm.example.invalid/v1", **api},
    )
    return value


def loads(tmp_path, value):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(value))
    return load(path)


@pytest.mark.parametrize(
    "build",
    [
        lambda s: s,
        lambda s: {**s, "model_provider": "bedrock"},
        lambda s: api_variant(s),
        lambda s: api_variant(s, base_url="https://llm.example.invalid"),
        lambda s: api_variant(s, base_url="https://api.example-llm.com/openai/v1/"),
        lambda s: api_variant(s, bytes_per_token=1),
        lambda s: api_variant(s, bytes_per_token=8),
        lambda s: api_variant(s, protocol="anthropic", base_url="https://api.example.invalid"),
        lambda s: {**api_variant(s), "model_id": "vendor/model-name:v1.2"},
    ],
)
def test_valid_model_provider_variants_load(spec, tmp_path, build):
    assert loads(tmp_path, build(spec))


@pytest.mark.parametrize(
    "build",
    [
        # Provider and its mandatory/forbidden blocks.
        lambda s: {k: v for k, v in s.items() if k != "model_arns"},
        lambda s: {
            **s,
            "model_provider": "bedrock",
            "model_api": {"protocol": "openai", "base_url": "https://a.example.invalid"},
        },
        lambda s: {**s, "model_api": {"protocol": "openai", "base_url": "https://a.example.invalid"}},
        lambda s: {**s, "model_provider": "openai"},
        lambda s: {**s, "model_provider": "model_api"},
        lambda s: {k: v for k, v in api_variant(s).items() if k != "model_api"},
        lambda s: {**api_variant(s), "model_arns": s["model_arns"]},
        lambda s: {**api_variant(s), "model_arns": []},
        # model_api object.
        lambda s: api_variant(s, protocol="gemini"),
        lambda s: api_variant(s, protocol=None),
        lambda s: api_variant(s, base_url="http://llm.example.invalid/v1"),
        lambda s: api_variant(s, base_url=f"https://{USERINFO}@llm.example.invalid/v1"),
        lambda s: api_variant(s, base_url="https://user@llm.example.invalid/v1"),
        lambda s: api_variant(s, base_url="https://llm.example.invalid/v1?key=secret"),
        lambda s: api_variant(s, base_url="https://llm.example.invalid/v1#frag"),
        lambda s: api_variant(s, base_url="https://llm.example.invalid:8443/v1"),
        lambda s: api_variant(s, base_url="https://203.0.113.7/v1"),
        lambda s: api_variant(s, base_url="https://localhost/v1"),
        lambda s: api_variant(s, base_url="https://llm.example.invalid/v1 /x"),
        lambda s: api_variant(s, base_url="llm.example.invalid/v1"),
        lambda s: api_variant(s, base_url=""),
        lambda s: api_variant(s, base_url="https://llm.example.invalid/v1\n"),
        lambda s: api_variant(s, bytes_per_token=0.5),
        lambda s: api_variant(s, bytes_per_token=9),
        lambda s: api_variant(s, bytes_per_token="3"),
        lambda s: api_variant(s, protocol="anthropic", bytes_per_token=3),
        lambda s: api_variant(s, extra="x"),
        lambda s: {k: v for k, v in api_variant(s).items() if k != "model_id"},
        # model_id for non-Bedrock providers is a conservative name, not a free string.
        lambda s: {**api_variant(s), "model_id": "has space"},
        lambda s: {**api_variant(s), "model_id": ".leading-dot"},
        lambda s: {**api_variant(s), "model_id": "x" * 129},
        lambda s: {**api_variant(s), "model_id": "model\nid"},
        lambda s: {**api_variant(s), "model_id": ""},
        lambda s: {**api_variant(s), "model_id": "model-id\n"},
        # Bedrock behaviour is unchanged.
        lambda s: {**s, "model_arns": []},
        lambda s: {**s, "model_id": "arn:aws:bedrock:eu-central-1::foundation-model/other.model"},
    ],
)
def test_invalid_model_provider_variants_fail_locally(spec, tmp_path, build):
    with pytest.raises(ValueError):
        loads(tmp_path, build(spec))


def test_model_api_diagnostics_do_not_echo_submitted_values(spec, tmp_path):
    with pytest.raises(ValueError) as error:
        loads(tmp_path, api_variant(spec, base_url=f"https://{USERINFO}@llm.example.invalid/v1"))
    assert USERINFO not in str(error.value) and "fixture-pw" not in str(error.value)


def test_catalog_and_cwagent_share_inventory(spec):
    from infra.spec import cwagent

    catalog = metric_catalog(spec)
    process = next(d for d in catalog if d["id"].endswith("process"))
    assert process["dimensions"]["exe"] == "nginx"
    assert process["dimensions"]["pid_finder"] == "native"
    config = cwagent(spec, spec["instances"][0])
    assert config["metrics"]["metrics_collected"]["procstat"][0]["exe"] == "nginx"
    assert all(
        i["log_group_name"] in log_groups(spec)
        for i in config["logs"]["logs_collected"]["files"]["collect_list"]
    )
    nginx = next(d for d in catalog if d["id"].endswith("nginx"))
    assert nginx["namespace"] == "tracaus/staging/Nginx" and nginx["dimensions"] == {}


def denied():
    return ClientError({"Error": {"Code": "AccessDenied", "Message": "private detail"}}, "Read")


def test_wrong_account_and_existing_release_block_writes(spec):
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "999999999999"}
    with pytest.raises(VerificationError):
        assert_account(sts, spec)
    cfn = MagicMock()
    with pytest.raises(VerificationError):
        assert_stack_absent(cfn, "existing")
    cfn.describe_stacks.side_effect = denied()
    with pytest.raises(ClientError):
        assert_stack_absent(cfn, "existing")


@pytest.mark.parametrize(
    "limit,requested,passes",
    [(10, 1, False), (100, 1, False), (102, 2, True), (101, 2, False), (10, None, True), (10, 0, True)],
)
def test_reserved_capacity_fails_closed(limit, requested, passes):
    client = MagicMock()
    client.get_account_settings.return_value = {"AccountLimit": {"UnreservedConcurrentExecutions": limit}}
    if passes:
        assert_concurrency(client, requested)
    else:
        with pytest.raises(VerificationError):
            assert_concurrency(client, requested)


def test_metric_discovery_paginated_exact_and_denied(spec):
    client = MagicMock()
    descriptor = alarm_descriptors(spec)[0]
    client.list_metrics.side_effect = [
        {"Metrics": [{"Dimensions": []}], "NextToken": "two"},
        {"Metrics": [{"Dimensions": [{"Name": "InstanceId", "Value": spec["instances"][0]["id"]}]}]},
    ]
    assert exact_metric_exists(client, descriptor)
    assert client.list_metrics.call_args.kwargs["NextToken"] == "two"
    client.list_metrics.side_effect = denied()
    with pytest.raises(ClientError):
        exact_metric_exists(client, descriptor)


def good_clients(spec):
    services = {name: MagicMock() for name in ("sts", "lambda", "ec2", "cloudwatch", "logs")}
    services["sts"].get_caller_identity.return_value = {"Account": spec["account_id"]}
    services["ec2"].describe_instances.return_value = {
        "Reservations": [
            {"Instances": [{"InstanceId": spec["instances"][0]["id"], "State": {"Name": "running"}}]}
        ]
    }
    descriptors = alarm_descriptors(spec)
    services["cloudwatch"].list_metrics.side_effect = lambda **kw: {
        "Metrics": [
            {"Dimensions": [{"Name": k, "Value": v} for k, v in d["dimensions"].items()]}
            for d in descriptors
            if d["metric_name"] == kw["MetricName"] and d["namespace"] == kw["Namespace"]
        ]
    }
    services["logs"].test_metric_filter.return_value = {"matches": [{"eventNumber": 1}]}
    return services, lambda service, region: services[service]


def test_coverage_missing_and_denied_are_failures(spec):
    services, factory = good_clients(spec)
    assert coverage(spec, factory)["status"] == "PASS"
    services["cloudwatch"].list_metrics.side_effect = None
    services["cloudwatch"].list_metrics.return_value = {"Metrics": []}
    with pytest.raises(VerificationError, match="Required metric"):
        coverage(spec, factory)
    services["cloudwatch"].list_metrics.side_effect = denied()
    with pytest.raises(ClientError):
        coverage(spec, factory)


def test_bad_nginx_fixture_blocks_promotion(spec):
    services, factory = good_clients(spec)
    services["logs"].test_metric_filter.return_value = {"matches": [{"eventNumber": 1}, {"eventNumber": 2}]}
    with pytest.raises(VerificationError, match="filter"):
        coverage(spec, factory)


def test_retirement_diff_preserves_unrelated_history_and_removes_old_recipient(spec):
    retired = {"type": "AWS::CloudWatch::Alarm", "id": "tracaus-staging-retired-cpu"}
    old = {
        "type": "AWS::SNS::Subscription",
        "id": topic_arn(spec, "reports") + ":old",
        "topic": topic_arn(spec, "reports"),
        "endpoint": "former@example.invalid",
        "protocol": "email",
    }
    history = {"type": "AWS::Logs::LogGroup", "id": "/tracaus/staging/retired/application"}
    plan = reconcile.plan(spec, [retired, old, history], "worker")
    assert plan["disable_alarms"] == [retired["id"]] and plan["unsubscribe"] == [old]
    assert plan["preserve"] == [history["id"]]
    with pytest.raises(VerificationError):
        reconcile.plan(spec, [{**old, "topic": "unrelated"}], "worker")


def test_feature_disable_and_instance_removal_have_explicit_retirements(spec):
    original = alarm_descriptors(spec)
    changed = copy.deepcopy(spec)
    changed["instances"][0].update(resource_alarms=False, nginx_alarm=False, process_exe=None)
    plan = reconcile.plan(
        changed, [{"type": "AWS::CloudWatch::Alarm", "id": a["alarm_name"]} for a in original], "worker"
    )
    assert len(plan["disable_alarms"]) == 5
    changed["instances"][0]["id"] = "i-11111111111111111"
    plan = reconcile.plan(
        changed, [{"type": "AWS::CloudWatch::Alarm", "id": a["alarm_name"]} for a in original], "worker"
    )
    assert len(plan["disable_alarms"]) == 6


def test_changed_retirement_plan_cannot_mutate(spec):
    factory = MagicMock()
    factory.return_value.get_caller_identity.return_value = {"Account": spec["account_id"]}
    with patch("infra.maintain.reconcile.owned_resources", return_value=[]), pytest.raises(VerificationError):
        reconcile.apply(spec, factory, {"review_hash": "stale"}, "worker")
    factory.return_value.unsubscribe.assert_not_called()
    factory.return_value.disable_alarm_actions.assert_not_called()


def test_receipt_requires_exact_recent_candidate(spec):
    bundle = {"review_hash": "this-release", "spec": spec}
    record = {
        "bundle_hash": "this-release",
        "status": "PASS",
        "canary_tools": ["fetch_logs", "fetch_metrics"],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    release.require_receipt(bundle, record)
    for change in (
        {"bundle_hash": "other"},
        {"status": "FAIL"},
        {"canary_tools": ["fetch_logs"]},
        {"checked_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()},
    ):
        with pytest.raises(VerificationError):
            release.require_receipt(bundle, {**record, **change})


def test_local_bundle_tamper_detected(spec, tmp_path):
    bundle = durable.render(
        ROOT / "examples/deployment.example.json", ROOT / "examples/durable.example.json", tmp_path
    )
    assert durable_ops.read_bundle(tmp_path, bundle["review_hash"])["spec"] == {
        **spec,
        "reference_only": True,
    }
    path = tmp_path / "foundation-tools.json"
    value = json.loads(path.read_text())
    value["Resources"]["Artifacts"]["Properties"]["BucketName"] = "unrelated"
    path.write_text(json.dumps(value))
    with pytest.raises(VerificationError, match="template"):
        durable_ops.read_bundle(tmp_path, bundle["review_hash"])


def test_cursor_uses_pinned_secret_and_does_not_refresh_to_current(monkeypatch):
    from tracaus import cursor

    cursor.secret_version.cache_clear()
    monkeypatch.setenv(
        "LOG_CURSOR_SECRET_ARN",
        "arn:aws:secretsmanager:eu-central-1:123456789012:secret:tracaus-staging/log-cursor-ABCDEF",
    )
    monkeypatch.setenv("LOG_CURSOR_SECRET_VERSION", "a" * 32)
    client = MagicMock()
    client.get_secret_value.return_value = {"SecretString": "x" * 48}
    with patch("boto3.client", return_value=client):
        assert cursor.key() == cursor.key()
    client.get_secret_value.assert_called_once()
    assert client.get_secret_value.call_args.kwargs["VersionId"] == "a" * 32
    cursor.secret_version.cache_clear()


def test_packaged_log_scope_rejects_cross_instance_before_aws(tmp_path, monkeypatch):
    from tests.helpers import body, load_lambda

    logs = load_lambda("fetch_logs")
    path = tmp_path / "scope.json"
    group = "/aiops/i-0123456789abcdef0/application"
    path.write_text(json.dumps([group]))
    monkeypatch.setenv("LOG_SCOPE_FILE", str(path))
    with patch("boto3.client") as client:
        response = logs.lambda_handler(
            {
                "parameters": [
                    {"name": "instance_id", "value": "i-11111111111111111"},
                    {"name": "log_group_name", "value": group},
                ]
            },
            None,
        )
    assert body(response)["status"] == "error"
    client.assert_not_called()


def test_changed_log_prefix_replaces_filters_and_retains_old_groups(spec):
    old = templates.foundation(spec, "monitor")
    changed = {**spec, "log_segment": "v2"}
    new = templates.foundation(changed, "monitor")
    assert set(log_groups(spec)).isdisjoint(log_groups(changed))
    assert all(
        r["DeletionPolicy"] == "Retain"
        for r in old["Resources"].values()
        if r["Type"] == "AWS::Logs::LogGroup"
    )
    assert all(
        "/staging/v2/" in r["Properties"]["LogGroupName"]
        for r in new["Resources"].values()
        if r["Type"] == "AWS::Logs::MetricFilter"
    )


def test_already_disabled_retired_alarm_does_not_block_routing(spec):
    item = {"type": "AWS::CloudWatch::Alarm", "id": "retired", "actions_enabled": False}
    assert reconcile.plan(spec, [item], "worker")["disable_alarms"] == []


def test_missing_receipt_fails_safely():
    with pytest.raises(VerificationError):
        release.require_receipt({"review_hash": "test"}, None)


def test_cloud_change_set_template_mismatch_cannot_execute(spec):
    bundle = {
        "spec": spec,
        "stages": {
            "tools": {
                "stack": "candidate-tools",
                "region": spec["bedrock_region"],
                "template_hash": "expected",
            }
        },
    }
    client = MagicMock()
    client.describe_change_set.return_value = {"Status": "CREATE_COMPLETE", "ExecutionStatus": "AVAILABLE"}
    client.get_template.return_value = {"TemplateBody": {"Resources": {}}}
    with pytest.raises(VerificationError, match="template"):
        release.inspect_change_set(bundle, "tools", "candidate", lambda *args: client)
    client.execute_change_set.assert_not_called()


def test_released_roles_are_retained_with_published_functions(spec):
    for stage in ("owned-tools",):
        for resource in examples(spec)[stage]["Resources"].values():
            if resource["Type"] == "AWS::IAM::Role":
                assert resource["DeletionPolicy"] == "Retain"


def test_reference_inventory_never_authenticates_to_aws(spec):
    client = MagicMock()
    with pytest.raises(VerificationError, match="Synthetic"):
        assert_account(client, {**spec, "reference_only": True})
    client.get_caller_identity.assert_not_called()


def test_maintenance_disables_automatic_ingress(spec):
    rendered = examples({**spec, "maintenance_mode": True, "reserved_concurrency": 0})["routing"]
    assert rendered["Resources"]["Ec2Down"]["Properties"]["State"] == "DISABLED"
    alarms = [r for k, r in rendered["Resources"].items() if k.startswith("Alarm")]
    assert alarms and all(r["Properties"]["ActionsEnabled"] is False for r in alarms)


def test_seal_requires_owned_successful_stack_and_reads_protection_back(spec, monkeypatch):
    from infra.deploy.durable_ops import seal_runtime
    from infra.spec import tags

    client = MagicMock()
    client.get_caller_identity.return_value = {"Account": spec["account_id"]}
    monkeypatch.setattr(durable_ops, "clients", lambda *args: client)
    monkeypatch.setattr(
        durable_ops.subprocess,
        "check_output",
        lambda argv, **kwargs: "reviewed-sha" if argv[1] == "rev-parse" else "",
    )
    stack = {
        "StackStatus": "CREATE_COMPLETE",
        "EnableTerminationProtection": True,
        "Tags": [{"Key": k, "Value": v} for k, v in tags(spec).items()],
    }
    client.describe_stacks.return_value = {"Stacks": [stack]}
    client.get_stack_policy.return_value = {"StackPolicyBody": json.dumps(release.SEALED_POLICY)}
    assert (
        seal_runtime(
            {"spec": spec, "source_dirty": False, "source_sha": "reviewed-sha"},
            "owned-tools",
            factory=lambda *args: client,
        )["status"]
        == "SEALED"
    )
    client.set_stack_policy.assert_called_once()
    client.update_termination_protection.assert_called_once()
    stack["EnableTerminationProtection"] = False
    with pytest.raises(VerificationError, match="did not take effect"):
        seal_runtime(
            {"spec": spec, "source_dirty": False, "source_sha": "reviewed-sha"},
            "owned-tools",
            factory=lambda *args: client,
        )
    client.reset_mock()
    stack["Tags"] = []
    with pytest.raises(VerificationError, match="owned"):
        seal_runtime(
            {"spec": spec, "source_dirty": False, "source_sha": "reviewed-sha"},
            "owned-tools",
            factory=lambda *args: client,
        )
    client.set_stack_policy.assert_not_called()


def test_routing_verification_rejects_alarm_and_event_drift(spec):
    from infra.deploy.verify import routing_health

    clients = {key: MagicMock() for key in ("events", "sns", "cloudwatch")}
    rendered = examples(spec)["routing"]["Resources"]
    worker = "arn:aws:sqs:eu-central-1:123456789012:ingress"
    rule = rendered["Ec2Down"]["Properties"]
    clients["events"].describe_rule.return_value = {
        "State": rule["State"],
        "EventPattern": json.dumps(rule["EventPattern"]),
    }
    clients["events"].list_targets_by_rule.return_value = {"Targets": rule["Targets"]}
    subscriptions = [r["Properties"] for r in rendered.values() if r["Type"] == "AWS::SNS::Subscription"]
    subscriptions.append({"Protocol": "sqs", "Endpoint": worker, "TopicArn": topic_arn(spec, "alarms")})
    clients["sns"].get_paginator.return_value.paginate.side_effect = lambda TopicArn: [
        {
            "Subscriptions": [
                {**s, "SubscriptionArn": TopicArn + ":subscription"}
                for s in subscriptions
                if s["TopicArn"] == TopicArn
            ]
        }
    ]
    alarms = [
        copy.deepcopy(r["Properties"]) for r in rendered.values() if r["Type"] == "AWS::CloudWatch::Alarm"
    ]
    clients["cloudwatch"].describe_alarms.return_value = {"MetricAlarms": alarms}

    def factory(key, region):
        return clients[key]

    assert routing_health(spec, factory, worker)["status"] == "PASS"
    alarms[0]["AlarmActions"] = ["arn:aws:sns:eu-central-1:123456789012:unrelated"]
    with pytest.raises(VerificationError, match="AlarmActions"):
        routing_health(spec, factory, worker)
    clients["events"].describe_rule.return_value["EventPattern"] = "{}"
    with pytest.raises(VerificationError, match="event selection"):
        routing_health(spec, factory, worker)


def test_changed_dependency_lock_requires_rebuild(spec, tmp_path):
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "python": "3.12",
                "architecture": "x86_64",
                "lock_sha256": "old-lock",
                "functions": {key: {} for key in ("fetch_logs", "fetch_metrics")},
            }
        )
    )
    with pytest.raises(VerificationError, match="Dependency lock changed"):
        release.checked_build(tmp_path, spec)
