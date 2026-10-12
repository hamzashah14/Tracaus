"""ECS services in the generated templates: the stop-event rule, the alarms, the policies and the ingress."""

import json

import pytest

from infra.spec import ROOT, load
from infra.stacks import templates
from scripts.dev import validate_durable

CONFIG = json.loads((ROOT / "examples/durable.example.json").read_text())
EC2 = "i-0123456789abcdef0"


def make(tmp_path, example="ecs", mixed=False, maintenance=False):
    value = json.loads((ROOT / f"examples/{example}.example.json").read_text())
    value["reference_only"] = False
    value["maintenance_mode"] = maintenance
    if mixed:
        ec2 = json.loads((ROOT / "examples/deployment.example.json").read_text())
        value["instances"], value["nginx_filters"] = ec2["instances"], ec2["nginx_filters"]
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(value))
    return load(path)


def resources(template, kind):
    return {k: v for k, v in template["Resources"].items() if v["Type"] == f"AWS::{kind}"}


def stages(spec):
    return validate_durable.examples(spec, CONFIG)


def test_an_ecs_only_deployment_has_the_ecs_rule_and_no_ec2_rule(tmp_path):
    spec = make(tmp_path)
    routing = templates.service_routing(spec)
    assert "Ec2Down" not in routing["Resources"]  # EventBridge refuses an empty instance list
    pattern = routing["Resources"]["EcsDown"]["Properties"]["EventPattern"]
    assert pattern == {
        "source": ["aws.ecs"],
        "detail-type": ["ECS Task State Change"],
        "detail": {
            "lastStatus": ["STOPPED"],
            "clusterArn": ["arn:aws:ecs:eu-central-1:123456789012:cluster/prod-cluster"],
            "group": ["service:orders-api", "service:worker"],
        },
    }
    assert routing["Resources"]["EcsDown"]["Properties"]["Name"] == "tracaus-staging-ecs-down"


def test_a_mixed_deployment_has_both_rules(tmp_path):
    routing = templates.service_routing(make(tmp_path, mixed=True))
    assert {"Ec2Down", "EcsDown"} <= routing["Resources"].keys()
    assert routing["Resources"]["Ec2Down"]["Properties"]["EventPattern"]["detail"]["instance-id"] == [EC2]


def test_the_ecs_rule_follows_maintenance_mode(tmp_path):
    state = templates.service_routing(make(tmp_path, maintenance=True))["Resources"]["EcsDown"]
    assert state["Properties"]["State"] == "DISABLED"


def test_alarms_exist_for_every_ecs_descriptor(tmp_path):
    spec = make(tmp_path)
    alarms = {
        a["Properties"]["AlarmName"]: a["Properties"]
        for a in resources(templates.service_routing(spec), "CloudWatch::Alarm").values()
    }
    assert set(alarms) == {
        "tracaus-staging-ecs-orders-cpu",
        "tracaus-staging-ecs-orders-memory",
        "tracaus-staging-ecs-orders-alb5xx",
        "tracaus-staging-ecs-orders-unhealthy",
        "tracaus-staging-ecs-worker-cpu",
        "tracaus-staging-ecs-worker-memory",
    }
    alb = alarms["tracaus-staging-ecs-orders-alb5xx"]
    assert alb["Namespace"] == "AWS/ApplicationELB" and alb["TreatMissingData"] == "notBreaching"
    assert {d["Name"] for d in alb["Dimensions"]} == {"LoadBalancer", "TargetGroup"}
    cpu = alarms["tracaus-staging-ecs-worker-cpu"]
    assert cpu["TreatMissingData"] == "missing" and cpu["EvaluationPeriods"] == 2


def test_alarm_descriptions_do_not_break_without_an_owner(tmp_path):
    spec = make(tmp_path, mixed=True)
    spec["observability"] = json.loads((ROOT / "examples/observability.example.json").read_text())[
        "observability"
    ]
    spec["nginx_filters"] = json.loads((ROOT / "examples/observability.example.json").read_text())[
        "nginx_filters"
    ]
    alarms = resources(templates.service_routing(spec), "CloudWatch::Alarm")
    named = {a["Properties"]["AlarmName"]: a["Properties"] for a in alarms.values()}
    assert "AlarmDescription" not in named["tracaus-staging-ecs-orders-cpu"]
    assert named[f"tracaus-staging-{EC2}-cpu"]["AlarmDescription"].startswith("Owner: ")


def test_the_alarm_topic_accepts_events_from_the_rules_that_exist(tmp_path):
    def sources(spec):
        policy = templates.foundation(spec, "monitor")["Resources"]["TopicPolicies"]["Properties"]
        statement = policy["PolicyDocument"]["Statement"][0]
        return statement["Condition"]["ArnEquals"]["aws:SourceArn"]

    base = "arn:aws:events:eu-central-1:123456789012:rule/tracaus-staging-"
    assert sources(make(tmp_path)) == base + "ecs-down"
    assert sources(make(tmp_path, mixed=True)) == [base + "ec2-down", base + "ecs-down"]
    assert sources(make(tmp_path, example="deployment")) == base + "ec2-down"


def test_the_ingress_learns_the_ecs_targets_and_allows_every_target(tmp_path):
    spec = make(tmp_path, mixed=True)
    runtime = stages(spec)["durable-runtime"]
    env = {}
    for resource in resources(runtime, "Lambda::Function").values():
        env.update(resource["Properties"]["Environment"]["Variables"])
    assert env["ALLOWED_INSTANCE_IDS"] == f"{EC2},ecs-orders,ecs-worker"
    ecs = json.loads(env["ECS_TARGETS"])
    assert ecs["ecs-orders"] == {
        "cluster_arn": "arn:aws:ecs:eu-central-1:123456789012:cluster/prod-cluster",
        "service": "orders-api",
    }  # only what the ingestion needs to match an event; the ALB details stay in the tools' packaged file
    assert set(ecs) == {"ecs-orders", "ecs-worker"}


def test_ec2_only_ingress_has_no_ecs_setting(tmp_path):
    runtime = stages(make(tmp_path, example="deployment"))["durable-runtime"]
    assert all(
        "ECS_TARGETS" not in r["Properties"]["Environment"]["Variables"]
        for r in resources(runtime, "Lambda::Function").values()
    )


@pytest.mark.parametrize("mixed", [False, True])
def test_every_rule_that_exists_has_a_dead_letter_queue_and_retry(tmp_path, mixed):
    routing = stages(make(tmp_path, mixed=mixed))["routing"]
    rules = resources(routing, "Events::Rule")
    ecs = rules["EcsDown"]["Properties"]["Targets"][0]
    assert ecs["RetryPolicy"] == {"MaximumEventAgeInSeconds": 600, "MaximumRetryAttempts": 5}
    assert "DeadLetterConfig" in ecs
    assert ("Ec2Down" in rules) is mixed
    if mixed:
        assert "DeadLetterConfig" in rules["Ec2Down"]["Properties"]["Targets"][0]


def test_the_delivery_dead_letter_queue_accepts_the_ecs_rule(tmp_path):
    foundation = stages(make(tmp_path))["durable-foundation"]
    policies = resources(foundation, "SQS::QueuePolicy")
    rules = [
        s["Condition"]["ArnEquals"]["aws:SourceArn"]
        for p in policies.values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if s["Principal"] == {"Service": "events.amazonaws.com"}
    ]
    assert rules == [
        [
            "arn:aws:events:eu-central-1:123456789012:rule/tracaus-staging-ecs-down",
            "arn:aws:events:eu-central-1:123456789012:rule/tracaus-staging-incident-sweep",
        ]
    ]


def many_services(tmp_path, count, name="orders-service-name", cluster="production-cluster"):
    value = json.loads((ROOT / "examples/ecs.example.json").read_text())
    value["reference_only"] = False
    balancer = value["ecs_services"][0]["load_balancer"]
    value["ecs_services"] = [
        {
            "id": f"ecs-s{n}",
            "cluster": cluster,
            "service": f"{name}-{n}",
            "load_balancer": balancer,
            "existing_log_groups": [{"name": f"/ecs/{name}-{n}"}],
        }
        for n in range(count)
    ]
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(value))
    return load(path)


def test_ten_alb_backed_services_still_render_the_ingestion_environment(tmp_path):
    runtime = stages(many_services(tmp_path, 10))[
        "durable-runtime"
    ]  # raises if the 4 KiB environment limit is hit
    env = next(
        r["Properties"]["Environment"]["Variables"]
        for r in resources(runtime, "Lambda::Function").values()
        if "ECS_TARGETS" in r["Properties"]["Environment"]["Variables"]
    )
    assert len(json.loads(env["ECS_TARGETS"])) == 10


def test_names_too_long_for_the_ingestion_setting_are_refused_when_the_file_loads(tmp_path):
    with pytest.raises(ValueError, match="too long in total"):
        many_services(tmp_path, 10, name="s" * 120, cluster="c" * 120)
