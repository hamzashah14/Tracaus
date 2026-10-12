"""fetch_metrics and fetch_logs for an ECS service: built-in names, scope and the whole-group read."""

import json
from unittest.mock import MagicMock, patch

import pytest

from tests.helpers import body, load_lambda

metrics = load_lambda("fetch_metrics")
logs = load_lambda("fetch_logs")

EC2 = "i-0123456789abcdef0"
ORDERS, WORKER = "ecs-orders", "ecs-worker"
TARGETS = {
    ORDERS: {
        "cluster": "prod-cluster",
        "service": "orders-api",
        "cluster_arn": "arn:aws:ecs:eu-central-1:123456789012:cluster/prod-cluster",
        "container_insights": False,
        "load_balancer": {
            "LoadBalancer": "app/orders-alb/50dc6c495c0c9188",
            "TargetGroup": "targetgroup/orders/73e2d6bc24d8a067",
            "target_group_arn": "arn:aws:elasticloadbalancing:eu-central-1:123456789012:targetgroup/orders/73e2d6bc24d8a067",
        },
    },
    WORKER: {
        "cluster": "prod-cluster",
        "service": "worker",
        "cluster_arn": "arn:aws:ecs:eu-central-1:123456789012:cluster/prod-cluster",
        "container_insights": True,
        "load_balancer": None,
    },
}
GROUP = "/ecs/orders-api"


@pytest.fixture
def deployed(tmp_path, monkeypatch):
    (tmp_path / "ecs.json").write_text(json.dumps(TARGETS))
    monkeypatch.setenv("ECS_TARGETS_FILE", str(tmp_path / "ecs.json"))
    monkeypatch.setenv("ALLOWED_INSTANCE_IDS", f"{EC2},{ORDERS},{WORKER}")
    return tmp_path


def fetch(response=None, **params):
    event = {"parameters": [{"name": k, "value": v} for k, v in params.items() if v is not None]}
    client = MagicMock()
    client.get_metric_statistics.return_value = response or {"Datapoints": []}
    with patch("boto3.client", return_value=client):
        result = body(metrics.lambda_handler(event, None))
    return result, client


def asked(client):
    return client.get_metric_statistics.call_args.kwargs


@pytest.mark.parametrize(
    ("metric", "namespace", "dimensions", "statistic"),
    [
        ("CPUUtilization", "AWS/ECS", ["ClusterName", "ServiceName"], "Average"),
        ("MemoryUtilization", "AWS/ECS", ["ClusterName", "ServiceName"], "Average"),
        ("HTTPCode_Target_5XX_Count", "AWS/ApplicationELB", ["LoadBalancer", "TargetGroup"], "Sum"),
        ("UnHealthyHostCount", "AWS/ApplicationELB", ["LoadBalancer", "TargetGroup"], "Maximum"),
        ("HealthyHostCount", "AWS/ApplicationELB", ["LoadBalancer", "TargetGroup"], "Minimum"),
        ("TargetResponseTime", "AWS/ApplicationELB", ["LoadBalancer", "TargetGroup"], "Average"),
    ],
)
def test_an_ecs_service_with_an_alb_reads_its_built_in_metrics(
    deployed, metric, namespace, dimensions, statistic
):
    result, client = fetch(instance_id=ORDERS, metric_name=metric)
    sent = asked(client)
    assert (sent["Namespace"], sent["Statistics"]) == (namespace, [statistic])
    assert [d["Name"] for d in sent["Dimensions"]] == dimensions
    assert result["instance_id"] == ORDERS and result["descriptor"]["instance_id"] == ORDERS


def test_the_dimension_values_come_from_the_packaged_scope(deployed):
    _, client = fetch(instance_id=ORDERS, metric_name="CPUUtilization")
    assert asked(client)["Dimensions"] == [
        {"Name": "ClusterName", "Value": "prod-cluster"},
        {"Name": "ServiceName", "Value": "orders-api"},
    ]
    _, client = fetch(instance_id=ORDERS, metric_name="RequestCount")
    assert asked(client)["Dimensions"] == [
        {"Name": "LoadBalancer", "Value": "app/orders-alb/50dc6c495c0c9188"},
        {"Name": "TargetGroup", "Value": "targetgroup/orders/73e2d6bc24d8a067"},
    ]


def test_the_default_metric_is_cpu(deployed):
    _, client = fetch(instance_id=WORKER)
    assert asked(client)["Namespace"] == "AWS/ECS" and asked(client)["MetricName"] == "CPUUtilization"


def test_alb_metrics_need_an_alb(deployed):
    result, client = fetch(instance_id=WORKER, metric_name="HTTPCode_Target_5XX_Count")
    assert result["status"] == "error"
    client.get_metric_statistics.assert_not_called()


def test_container_insights_metrics_need_the_flag(deployed):
    result, client = fetch(instance_id=ORDERS, metric_name="RunningTaskCount")
    assert result["status"] == "error"
    client.get_metric_statistics.assert_not_called()
    _, client = fetch(instance_id=WORKER, metric_name="RunningTaskCount")
    assert asked(client)["Namespace"] == "ECS/ContainerInsights"


@pytest.mark.parametrize(
    ("target", "params"),
    [
        (ORDERS, {"metric_name": "mem_used_percent"}),  # an EC2 agent metric
        (ORDERS, {"metric_name": "StatusCheckFailed"}),
        (EC2, {"metric_name": "MemoryUtilization"}),  # an ECS metric on an instance
        (EC2, {"metric_name": "UnHealthyHostCount"}),
        (ORDERS, {"metric_name": "CPUUtilization", "namespace": "Custom/App"}),
        (ORDERS, {"metric_name": "CPUUtilization", "namespace": "AWS/EC2"}),
        (ORDERS, {"metric_name": "CPUUtilization", "path": "/"}),
        (ORDERS, {"metric_name": "CPUUtilization", "statistic": "p99"}),
        ("ecs-unlisted", {"metric_name": "CPUUtilization"}),
        ("ecs-has-hyphen", {"metric_name": "CPUUtilization"}),
    ],
)
def test_names_and_scope_that_are_not_allowed_never_reach_cloudwatch(deployed, target, params):
    result, client = fetch(instance_id=target, **params)
    assert result["status"] == "error"
    client.get_metric_statistics.assert_not_called()


def test_a_listed_target_missing_from_the_packaged_scope_is_refused(deployed, monkeypatch):
    monkeypatch.setenv("ALLOWED_INSTANCE_IDS", f"{EC2},{ORDERS},ecs-new")
    result, client = fetch(instance_id="ecs-new", metric_name="CPUUtilization")
    assert result["status"] == "error" and "redeploy" in result["message"]
    client.get_metric_statistics.assert_not_called()


def test_ec2_metrics_are_unchanged(deployed):
    _, client = fetch(instance_id=EC2, metric_name="CPUUtilization")
    assert asked(client)["Namespace"] == "AWS/EC2"
    assert asked(client)["Dimensions"] == [{"Name": "InstanceId", "Value": EC2}]


def test_no_data_for_ecs_says_what_to_check(deployed):
    result, _ = fetch(instance_id=ORDERS, metric_name="CPUUtilization")
    assert result["status"] == "no_data" and "Container Insights" in result["message"]


def test_a_configured_ecs_metric_id_resolves_through_the_catalog(deployed, tmp_path, monkeypatch):
    entry = {
        "id": "ecs-orders-alb5xx",
        "instance_id": ORDERS,
        "namespace": "AWS/ApplicationELB",
        "metric_name": "HTTPCode_Target_5XX_Count",
        "statistic": "Sum",
        "dimensions": {
            "LoadBalancer": "app/orders-alb/50dc6c495c0c9188",
            "TargetGroup": "targetgroup/orders/73e2d6bc24d8a067",
        },
    }
    (tmp_path / "catalog.json").write_text(json.dumps([entry]))
    monkeypatch.setenv("METRIC_CATALOG_FILE", str(tmp_path / "catalog.json"))
    _, client = fetch(instance_id=ORDERS, metric_id="ecs-orders-alb5xx")
    assert asked(client)["Namespace"] == "AWS/ApplicationELB"
    result, client = fetch(instance_id=WORKER, metric_id="ecs-orders-alb5xx")  # another service's descriptor
    assert result["status"] == "error"
    client.get_metric_statistics.assert_not_called()


@pytest.fixture
def groups(tmp_path, monkeypatch, deployed):
    (tmp_path / "scope.json").write_text("[]")  # an ECS-only deployment creates no Tracaus log groups
    (tmp_path / "existing.json").write_text(json.dumps({ORDERS: {GROUP: "all"}}))
    monkeypatch.setenv("LOG_SCOPE_FILE", str(tmp_path / "scope.json"))
    monkeypatch.setenv("EXISTING_LOG_GROUPS_FILE", str(tmp_path / "existing.json"))


def search(params, client=None):
    event = {"parameters": [{"name": k, "value": v} for k, v in params.items()]}
    with patch("boto3.client", return_value=client or MagicMock()):
        return body(logs.lambda_handler(event, None))


def insights():
    client = MagicMock()
    client.start_query.return_value = {"queryId": "q"}
    client.get_query_results.return_value = {"status": "Complete", "results": []}
    client.describe_log_groups.return_value = {"logGroups": []}
    return client


def test_discovery_lists_the_services_existing_groups(groups):
    result = search({"instance_id": ORDERS}, insights())
    assert result["status"] == "log_groups_found" and result["log_groups"] == [GROUP]


def test_an_ecs_group_is_read_whole_not_filtered_by_target_id(groups):
    client = insights()
    result = search({"instance_id": ORDERS, "log_group_name": GROUP}, client)
    assert result["status"] == "no_matching_lines" and result["log_group"] == GROUP
    assert all("@logStream" not in c.kwargs["queryString"] for c in client.start_query.call_args_list)
    assert {c.kwargs["logGroupName"] for c in client.start_query.call_args_list} == {GROUP}


def test_another_service_cannot_read_it_and_nothing_reaches_aws(groups):
    client = insights()
    assert search({"instance_id": WORKER, "log_group_name": GROUP}, client)["status"] == "error"
    assert search({"instance_id": EC2, "log_group_name": GROUP}, client)["status"] == "error"
    client.start_query.assert_not_called()


def test_a_service_with_no_readable_logs_gets_a_clear_empty_answer(tmp_path, monkeypatch, deployed):
    (tmp_path / "scope.json").write_text("[]")
    monkeypatch.setenv("LOG_SCOPE_FILE", str(tmp_path / "scope.json"))
    monkeypatch.delenv("EXISTING_LOG_GROUPS_FILE", raising=False)
    result = search({"instance_id": WORKER}, insights())
    assert result["status"] == "no_log_groups_found" and result["log_groups"] == []


def test_an_ecs_id_is_accepted_and_a_bad_one_is_not(groups):
    assert search({"instance_id": "ecs-has-hyphen", "log_group_name": GROUP}, insights())["status"] == "error"
    assert search({"instance_id": "orders"}, insights())["status"] == "error"


def packaged_tools(tmp_path, spec, ecs=None, functions=None):
    """A build directory as scripts/build_lambdas.py would write it, minus the downloaded wheels."""
    import hashlib
    import zipfile

    from infra.spec import ROOT, existing_log_groups, log_groups, metric_catalog, tool_functions

    names = {str(p.relative_to(ROOT)) for p in (ROOT / "tracaus").glob("*.py")} | {
        "agent-instruction.txt",
        "schemas/fetch_logs.json",
        "schemas/fetch_metrics.json",
        "tracaus_agentcore.py",
        *(["schemas/fetch_ecs.json"] if ecs else []),
    }
    lock = (ROOT / "requirements/lambda.lock").read_bytes()
    files = {name: (ROOT / name).read_bytes() for name in names}
    files["requirements/lambda.lock"] = lock
    files["config/metric-catalog.json"] = json.dumps(metric_catalog(spec)).encode()
    files["config/log-scope.json"] = json.dumps(log_groups(spec)).encode()
    if existing_log_groups(spec):
        files["config/existing-log-groups.json"] = json.dumps(existing_log_groups(spec)).encode()
    if ecs is not None:
        files["config/ecs-targets.json"] = json.dumps(ecs).encode()
    manifest = {
        "python": "3.12",
        "architecture": "x86_64",
        "lock_sha256": hashlib.sha256(lock).hexdigest(),
        "functions": {},
    }
    for function in functions or tool_functions(spec):
        members = {
            **files,
            "lambda_function.py": (ROOT / "lambda" / function / "lambda_function.py").read_bytes(),
        }
        with zipfile.ZipFile(tmp_path / f"{function}.zip", "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        manifest["functions"][function] = {
            "artifact": f"{function}.zip",
            "sha256": hashlib.sha256((tmp_path / f"{function}.zip").read_bytes()).hexdigest(),
            "source_files": {
                n: hashlib.sha256(d).hexdigest()
                for n, d in members.items()
                if n != "requirements/lambda.lock"
            },
        }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))


def test_the_release_check_binds_the_packaged_ecs_targets_to_the_inventory(tmp_path):
    from infra import release
    from infra.spec import ROOT, ecs_targets, load
    from infra.verify import VerificationError

    raw = json.loads((ROOT / "examples/ecs.example.json").read_text())
    raw["reference_only"] = False
    (tmp_path / "d.json").write_text(json.dumps(raw))
    spec = load(tmp_path / "d.json")
    build = tmp_path / "build"
    build.mkdir()
    packaged_tools(build, spec, ecs=ecs_targets(spec))
    assert set(release.checked_build(build, spec)["functions"]) == {
        "fetch_logs",
        "fetch_metrics",
        "fetch_ecs",
    }

    packaged_tools(
        build,
        spec,
        ecs={**ecs_targets(spec), "ecs-orders": {**ecs_targets(spec)["ecs-orders"], "service": "other"}},
    )
    with pytest.raises(VerificationError, match="ECS targets differ"):
        release.checked_build(build, spec)  # a different service than the inventory declares
    packaged_tools(build, spec, ecs=None)
    with pytest.raises(VerificationError):
        release.checked_build(build, spec)  # declared but not packaged
    packaged_tools(build, spec, ecs=ecs_targets(spec), functions=("fetch_logs", "fetch_metrics"))
    with pytest.raises(VerificationError, match="every read-only tool"):
        release.checked_build(build, spec)  # the third tool is missing

    plain = load(ROOT / "examples/deployment.example.json")
    packaged_tools(build, plain, ecs=ecs_targets(spec))
    with pytest.raises(VerificationError, match="ECS targets differ"):
        release.checked_build(build, plain)  # a stray file the inventory does not declare
    packaged_tools(build, plain)
    assert set(release.checked_build(build, plain)["functions"]) == {"fetch_logs", "fetch_metrics"}
