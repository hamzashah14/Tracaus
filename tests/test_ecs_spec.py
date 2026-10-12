"""ECS services in deployment.json: the load rules, the ALB question and the generated alarms."""

import copy
import json

import pytest

from infra.spec import (
    ROOT,
    alarm_descriptors,
    ecs_targets,
    existing_log_groups,
    load,
    metric_catalog,
    targets,
    tool_functions,
)

LB = "arn:aws:elasticloadbalancing:eu-central-1:123456789012:loadbalancer/app/orders-alb/50dc6c495c0c9188"
TG = "arn:aws:elasticloadbalancing:eu-central-1:123456789012:targetgroup/orders/73e2d6bc24d8a067"
EC2 = "i-0123456789abcdef0"


def raw(example="ecs"):
    value = json.loads((ROOT / f"examples/{example}.example.json").read_text())
    value["reference_only"] = False
    return value


def write(tmp_path, value):
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(value))
    return path


def make(tmp_path, change=None, example="ecs"):
    value = raw(example)
    if change:
        change(value)
    return load(write(tmp_path, value))


def service(**extra):
    base = {"id": "ecs-api", "cluster": "c", "service": "api", "load_balancer": None}
    return {**base, **extra}


def test_an_ecs_only_deployment_loads(tmp_path):
    spec = make(tmp_path)
    assert targets(spec) == ["ecs-orders", "ecs-worker"]
    assert tool_functions(spec) == ("fetch_logs", "fetch_metrics", "fetch_ecs")


def test_ec2_only_deployments_keep_two_tools_and_no_ecs_data(tmp_path):
    spec = make(tmp_path, example="deployment")
    assert tool_functions(spec) == ("fetch_logs", "fetch_metrics")
    assert ecs_targets(spec) == {}


def test_a_mixed_deployment_lists_ec2_first(tmp_path):
    def add(value):
        value["instances"] = raw("deployment")["instances"]
        value["nginx_filters"] = raw("deployment")["nginx_filters"]

    assert targets(make(tmp_path, add)) == [EC2, "ecs-orders", "ecs-worker"]


def test_the_alb_question_must_be_answered(tmp_path):
    def drop(value):
        del value["ecs_services"][1]["load_balancer"]

    with pytest.raises(ValueError, match="ecs_services"):
        make(tmp_path, drop)


def test_no_alb_means_no_alb_alarms_and_no_alb_dimensions(tmp_path):
    spec = make(tmp_path)
    ids = {d["id"] for d in alarm_descriptors(spec)}
    assert {"ecs-orders-alb5xx", "ecs-orders-unhealthy"} <= ids
    assert not {"ecs-worker-alb5xx", "ecs-worker-unhealthy"} & ids
    assert ecs_targets(spec)["ecs-worker"]["load_balancer"] is None


def test_alb_dimension_values_come_from_the_arns(tmp_path):
    balancer = ecs_targets(make(tmp_path))["ecs-orders"]["load_balancer"]
    assert balancer == {
        "LoadBalancer": "app/orders-alb/50dc6c495c0c9188",
        "TargetGroup": "targetgroup/orders/73e2d6bc24d8a067",
        "target_group_arn": TG,
    }


def test_ecs_alarm_descriptors(tmp_path):
    by_id = {d["id"]: d for d in alarm_descriptors(make(tmp_path))}
    cpu = by_id["ecs-orders-cpu"]
    assert (cpu["namespace"], cpu["metric_name"], cpu["threshold"], cpu["period"]) == (
        "AWS/ECS",
        "CPUUtilization",
        90,
        300,
    )
    assert cpu["dimensions"] == {"ClusterName": "prod-cluster", "ServiceName": "orders-api"}
    assert cpu["instance_id"] == "ecs-orders" and cpu["alarm_name"] == "tracaus-staging-ecs-orders-cpu"
    alb = by_id["ecs-orders-alb5xx"]
    assert alb["missing_data"] == "notBreaching" and alb["dimensions"]["TargetGroup"].startswith(
        "targetgroup/"
    )
    assert by_id["ecs-orders-unhealthy"]["comparison"] == "GreaterThanThreshold"


def test_container_insights_is_opt_in(tmp_path):
    assert "ecs-orders-running" not in {d["id"] for d in alarm_descriptors(make(tmp_path))}

    def on(value):
        value["ecs_services"][0]["container_insights"] = True

    running = next(d for d in alarm_descriptors(make(tmp_path, on)) if d["id"] == "ecs-orders-running")
    assert (running["namespace"], running["metric_name"], running["comparison"]) == (
        "ECS/ContainerInsights",
        "RunningTaskCount",
        "LessThanThreshold",
    )


def test_resource_alarms_can_be_switched_off(tmp_path):
    def off(value):
        value["ecs_services"][1]["resource_alarms"] = False

    ids = {d["id"] for d in alarm_descriptors(make(tmp_path, off))}
    assert "ecs-worker-cpu" not in ids and "ecs-orders-cpu" in ids


def test_the_metric_catalog_covers_ecs_targets(tmp_path):
    entries = metric_catalog(make(tmp_path))
    assert {e["instance_id"] for e in entries} == {"ecs-orders", "ecs-worker"}


def test_ecs_log_groups_are_read_whole(tmp_path):
    assert existing_log_groups(make(tmp_path)) == {
        "ecs-orders": {"/ecs/orders-api": "all"},
        "ecs-worker": {"/ecs/worker": "all"},
    }


def test_logs_are_optional_for_ecs(tmp_path):
    def none(value):
        del value["ecs_services"][1]["existing_log_groups"]

    assert "ecs-worker" not in existing_log_groups(make(tmp_path, none))


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v["ecs_services"][0]["load_balancer"].update(
            load_balancer_arn=LB.replace("loadbalancer/app/", "loadbalancer/net/")
        ),
        lambda v: v["ecs_services"][0]["load_balancer"].update(
            load_balancer_arn=LB.replace("eu-central-1", "us-east-1")
        ),
        lambda v: v["ecs_services"][0]["load_balancer"].update(
            target_group_arn=TG.replace("123456789012", "999999999999")
        ),
        lambda v: v["ecs_services"][0]["load_balancer"].pop("target_group_arn"),
        lambda v: v["ecs_services"][0].update(id="ecs-has-hyphen"),
        lambda v: v["ecs_services"][0].update(id="orders"),
        lambda v: v["ecs_services"][0].update(id="ecs-" + "a" * 25),
        lambda v: v["ecs_services"][0].update(cluster="has space"),
        lambda v: v["ecs_services"][1].update(id="ecs-orders"),
        lambda v: v["ecs_services"][1].update(cluster="prod-cluster", service="orders-api"),
        lambda v: v["ecs_services"][1].update(existing_log_groups=[{"name": "/ecs/orders-api"}]),
        lambda v: v["ecs_services"][0].update(existing_log_groups=[{"name": "/tracaus/staging/x"}]),
        lambda v: v["ecs_services"][0].update(existing_log_groups=[{"name": "/a"}, {"name": "/a"}]),
        lambda v: v["ecs_services"][0].update(existing_log_groups=[{"name": "/a", "streams": "instance"}]),
        lambda v: v["ecs_services"][0].update(unknown=True),
        lambda v: v.update(ecs_services=[service(id=f"ecs-s{i}", service=f"s{i}") for i in range(11)]),
        lambda v: v.update(ecs_services=[]),
    ],
)
def test_bad_ecs_entries_are_rejected_without_echoing_values(tmp_path, change):
    with pytest.raises(ValueError) as error:
        make(tmp_path, change)
    for secret in ("999999999999", "has space", "/tracaus/staging/x", "ecs-has-hyphen"):
        assert secret not in str(error.value)


def test_an_id_may_not_repeat_an_ec2_id(tmp_path):
    def clash(value):
        value["instances"] = raw("deployment")["instances"]
        value["nginx_filters"] = raw("deployment")["nginx_filters"]
        value["ecs_services"][0]["id"] = EC2

    with pytest.raises(ValueError):
        make(tmp_path, clash)


def test_ten_targets_in_total_is_the_limit(tmp_path):
    def many(value):
        value["instances"] = raw("deployment")["instances"]
        value["nginx_filters"] = raw("deployment")["nginx_filters"]
        value["ecs_services"] = [service(id=f"ecs-s{i}", service=f"s{i}") for i in range(9)]

    assert len(targets(make(tmp_path, many))) == 10

    def too_many(value):
        many(value)
        value["ecs_services"].append(service(id="ecs-extra", service="extra"))

    with pytest.raises(ValueError, match="At most 10"):
        make(tmp_path, too_many)


def test_nginx_filters_are_needed_only_with_an_nginx_alarm(tmp_path):
    def instance_without_filters(value):
        value["instances"] = copy.deepcopy(raw("deployment")["instances"])

    with pytest.raises(ValueError, match="nginx_filters"):
        make(tmp_path, instance_without_filters)

    def instance_without_nginx(value):
        value["instances"] = copy.deepcopy(raw("deployment")["instances"])
        value["instances"][0]["nginx_alarm"] = False

    assert EC2 in targets(make(tmp_path, instance_without_nginx))
