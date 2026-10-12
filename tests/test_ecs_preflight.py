"""ECS in `check`, the coverage check, the operator IAM policy and `ecs-discover`.

The ECS and ELB clients are real botocore clients behind a Stubber, so every call and every response is
validated against AWS's own service model."""

import json
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from infra.deploy import deployment_preflight, ecs_discover
from infra.deploy.verify import VerificationError, assert_ecs_inventory, coverage
from infra.spec import ROOT, load
from infra.stacks import bootstrap_iam

REGION, ACCOUNT = "eu-central-1", "123456789012"
LB = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/orders-alb/50dc6c495c0c9188"
OTHER_LB = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/other/aaaaaaaaaaaaaaaa"
NLB = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/net/orders/bbbbbbbbbbbbbbbb"
TG = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/orders/73e2d6bc24d8a067"
OTHER_TG = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/other/cccccccccccccccc"


def client(service):
    session = boto3.session.Session(
        aws_access_key_id="synthetic", aws_secret_access_key="synthetic", region_name=REGION
    )
    real = session.client(service)
    return real, Stubber(real)


@pytest.fixture
def spec(tmp_path):
    value = json.loads((ROOT / "examples/ecs.example.json").read_text())
    value["reference_only"] = False
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(value))
    return load(path)


def service(name, status="ACTIVE", groups=(), task_definition="arn:aws:ecs:x:1:task-definition/f:1"):
    return {
        "serviceName": name,
        "status": status,
        "taskDefinition": task_definition,
        "loadBalancers": [{"targetGroupArn": arn} for arn in groups],
    }


def stubbed(spec, services, groups=None, failures=()):
    """A factory whose `ecs` and `elbv2` clients answer `check` for the example's two services."""
    ecs, ecs_stub = client("ecs")
    ecs_stub.add_response(
        "describe_services",
        {"services": services, "failures": list(failures)},
        {"cluster": "prod-cluster", "services": ["orders-api", "worker"]},
    )
    elb, elb_stub = client("elbv2")
    if groups is not None:
        elb_stub.add_response(
            "describe_target_groups",
            {"TargetGroups": [{"TargetGroupArn": TG, "LoadBalancerArns": groups}]},
            {"TargetGroupArns": [TG]},
        )
    ecs_stub.activate(), elb_stub.activate()
    return {"ecs": ecs, "elbv2": elb}, (ecs_stub, elb_stub)


def good(spec):
    return stubbed(spec, [service("orders-api", groups=[TG]), service("worker")], [LB])


def factory(clients):
    return lambda name, region: clients[name]


def test_matching_services_and_alb_pass(spec):
    clients, stubs = good(spec)
    assert_ecs_inventory(spec, factory(clients))
    for stub in stubs:
        stub.assert_no_pending_responses()


def test_a_service_without_an_alb_makes_no_elb_call(spec):
    clients, _ = stubbed(spec, [service("orders-api", groups=[TG]), service("worker")], [LB])
    spec["ecs_services"][0]["load_balancer"] = None
    assert_ecs_inventory(spec, factory({"ecs": clients["ecs"]}))  # no `elbv2` client exists to call


@pytest.mark.parametrize(
    ("services", "message"),
    [
        ([service("worker")], "ecs-orders"),  # not returned
        ([service("orders-api", "INACTIVE", [TG]), service("worker")], "ecs-orders"),
        ([service("orders-api", "DRAINING", [TG]), service("worker")], "ecs-orders"),
        ([service("orders-api", groups=[TG]), service("worker", "INACTIVE")], "ecs-worker"),
    ],
)
def test_missing_or_inactive_services_block(spec, services, message):
    clients, _ = stubbed(spec, services, [LB])
    with pytest.raises(VerificationError, match=message):
        assert_ecs_inventory(spec, factory(clients))


def test_a_missing_cluster_blocks(spec):
    ecs, stub = client("ecs")
    stub.add_client_error("describe_services", "ClusterNotFoundException", "no", 400)
    stub.activate()
    with pytest.raises(VerificationError, match="ecs-orders"):
        assert_ecs_inventory(spec, factory({"ecs": ecs}))


def test_other_errors_are_not_read_as_absence(spec):
    ecs, stub = client("ecs")
    stub.add_client_error("describe_services", "AccessDeniedException", "no", 403)
    stub.activate()
    with pytest.raises(ClientError):
        assert_ecs_inventory(spec, factory({"ecs": ecs}))


def test_an_alb_the_service_does_not_use_blocks_and_says_what_it_uses(spec):
    clients, _ = stubbed(spec, [service("orders-api", groups=[OTHER_TG]), service("worker")])
    with pytest.raises(VerificationError, match="ecs-orders") as error:
        assert_ecs_inventory(spec, factory(clients))
    assert OTHER_TG in str(error.value)


def test_a_target_group_of_another_load_balancer_blocks(spec):
    clients, _ = stubbed(spec, [service("orders-api", groups=[TG]), service("worker")], [OTHER_LB])
    with pytest.raises(VerificationError, match="not the declared load_balancer_arn"):
        assert_ecs_inventory(spec, factory(clients))


def test_ec2_only_deployments_make_no_ecs_calls():
    value = json.loads((ROOT / "examples/deployment.example.json").read_text())
    value["reference_only"] = False
    assert_ecs_inventory(value, factory({}))  # any client lookup would raise KeyError


def coverage_clients(spec):
    services = {name: MagicMock() for name in ("sts", "lambda", "cloudwatch", "logs")}
    services["sts"].get_caller_identity.return_value = {"Account": ACCOUNT}
    present = {"AWS/ECS", "AWS/ApplicationELB"}
    services["cloudwatch"].list_metrics.side_effect = lambda **kw: {
        "Metrics": [
            {"Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()]}
            for dims in (
                {"ClusterName": "prod-cluster", "ServiceName": "orders-api"},
                {"ClusterName": "prod-cluster", "ServiceName": "worker"},
                {
                    "LoadBalancer": "app/orders-alb/50dc6c495c0c9188",
                    "TargetGroup": "targetgroup/orders/73e2d6bc24d8a067",
                },
            )
            if kw["Namespace"] in present
        ]
    }
    services["logs"].describe_log_groups.side_effect = lambda logGroupNamePrefix: {
        "logGroups": [{"logGroupName": logGroupNamePrefix}]
    }
    return services


def test_coverage_for_an_ecs_only_deployment(spec):
    clients, _ = good(spec)
    clients.update(coverage_clients(spec))  # note: no `ec2` client at all
    result = coverage(spec, factory(clients))
    status = {m["id"]: m["status"] for m in result["metrics"]}
    assert status["ecs-orders-cpu"] == "present" and status["ecs-orders-unhealthy"] == "present"
    # An ALB reports 5xx counts only when above zero, so a healthy service must still pass.
    assert status["ecs-orders-alb5xx"] == "reported_only_when_nonzero"


def test_coverage_still_requires_the_default_service_metrics(spec):
    clients, _ = good(spec)
    clients.update(coverage_clients(spec))
    clients["cloudwatch"].list_metrics.side_effect = None
    clients["cloudwatch"].list_metrics.return_value = {"Metrics": []}
    with pytest.raises(VerificationError, match="Required metric unavailable: ecs-orders-cpu"):
        coverage(spec, factory(clients))


def actions(statements):
    return {a for s in statements for a in s["Action"]}


def test_the_operator_may_describe_ecs_only_when_the_deployment_has_ecs(spec):
    ec2 = json.loads((ROOT / "examples/deployment.example.json").read_text())
    needed = {
        "ecs:DescribeServices",
        "ecs:DescribeTaskDefinition",
        "elasticloadbalancing:DescribeTargetGroups",
    }
    assert needed <= actions(bootstrap_iam.operator_policy(spec))
    assert not needed & actions(bootstrap_iam.operator_policy(ec2))


def test_the_preflight_simulates_the_ecs_reads(spec):
    config = json.loads((ROOT / "examples/durable.example.json").read_text())
    requested = {a for r in deployment_preflight.permission_requests(spec, config) for a in r["actions"]}
    assert {"ecs:DescribeServices", "elasticloadbalancing:DescribeTargetGroups"} <= requested
    no_alb = json.loads(json.dumps(spec))
    for item in no_alb["ecs_services"]:
        item["load_balancer"] = None
    again = {a for r in deployment_preflight.permission_requests(no_alb, config) for a in r["actions"]}
    assert "elasticloadbalancing:DescribeTargetGroups" not in again and "ecs:DescribeServices" in again


def task_definition(*containers):
    return {
        "taskDefinition": {
            "family": "orders",
            "containerDefinitions": [{"name": name, "image": "x", **extra} for name, extra in containers],
        }
    }


def discovery(raw, services, definition, balancer=None):
    ecs, ecs_stub = client("ecs")
    for item in raw["ecs_services"]:  # the order discover calls them in: service, then its task definition
        ecs_stub.add_response(
            "describe_services",
            {"services": services.get(item["service"], []), "failures": []},
            {"cluster": item["cluster"], "services": [item["service"]]},
        )
        if services.get(item["service"]):
            ecs_stub.add_response(
                "describe_task_definition",
                definition,
                {"taskDefinition": "arn:aws:ecs:x:1:task-definition/f:1"},
            )
    elb, elb_stub = client("elbv2")
    for arn in balancer or []:
        elb_stub.add_response(
            "describe_target_groups",
            {"TargetGroups": [{"TargetGroupArn": arn[0], "LoadBalancerArns": arn[1]}]},
            {"TargetGroupArns": [arn[0]]},
        )
    ecs_stub.activate(), elb_stub.activate()
    return ecs_discover.discover(raw, factory({"ecs": ecs, "elbv2": elb}))


def awslogs(group):
    return {
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {"awslogs-group": group, "awslogs-stream-prefix": "ecs"},
        }
    }


def draft(*entries):
    return {"monitor_region": REGION, "account_id": ACCOUNT, "ecs_services": list(entries)}


def entry(id="ecs-orders", service="orders-api", **extra):
    return {"id": id, "cluster": "prod-cluster", "service": service, **extra}


def test_discovery_finds_the_default_log_group_and_the_alb():
    found = discovery(
        draft(entry()),  # a draft: no load_balancer or log groups written yet
        {"orders-api": [service("orders-api", groups=[TG])]},
        task_definition(("web", awslogs("/ecs/orders"))),
        [(TG, [LB])],
    )[0]
    assert found["existing_log_groups"] == [{"name": "/ecs/orders"}]
    assert found["load_balancer"] == {"load_balancer_arn": LB, "target_group_arn": TG}
    assert found["task_definition"] == "f:1" and found["notes"] == []


def test_discovery_says_when_there_is_no_alb():
    found = discovery(
        draft(entry()),
        {"orders-api": [service("orders-api")]},
        task_definition(("web", awslogs("/ecs/orders"))),
    )[0]
    assert found["load_balancer"] is None and "No load balancer" in found["notes"][0]


def test_discovery_refuses_to_suggest_a_network_load_balancer():
    found = discovery(
        draft(entry()),
        {"orders-api": [service("orders-api", groups=[TG])]},
        task_definition(("web", awslogs("/ecs/orders"))),
        [(TG, [NLB])],
    )[0]
    assert found["load_balancer"] is None and "not an ALB" in found["notes"][0]


def test_discovery_reports_other_log_drivers_and_missing_logs():
    found = discovery(
        draft(entry()),
        {"orders-api": [service("orders-api")]},
        task_definition(("web", {"logConfiguration": {"logDriver": "splunk"}}), ("side", {})),
    )[0]
    assert found["existing_log_groups"] == []
    assert any("splunk" in n and "none" in n for n in found["notes"]) and any(
        "No awslogs" in n for n in found["notes"]
    )


def test_discovery_collects_sidecar_groups_once_each():
    found = discovery(
        draft(entry()),
        {"orders-api": [service("orders-api")]},
        task_definition(
            ("web", awslogs("/ecs/orders")), ("side", awslogs("/ecs/orders")), ("log", awslogs("/ecs/side"))
        ),
    )[0]
    assert [g["name"] for g in found["existing_log_groups"]] == ["/ecs/orders", "/ecs/side"]


def test_discovery_compares_with_what_deployment_json_already_says():
    found = discovery(
        draft(entry(load_balancer=None, existing_log_groups=[{"name": "/old"}])),
        {"orders-api": [service("orders-api", groups=[TG])]},
        task_definition(("web", awslogs("/ecs/orders"))),
        [(TG, [LB])],
    )[0]
    text = " ".join(found["notes"])
    assert "says no ALB" in text and "different log groups" in text


def test_discovery_flags_two_services_sharing_a_group():
    raw = draft(entry(), entry("ecs-worker", "worker"))
    services = {"orders-api": [service("orders-api")], "worker": [service("worker")]}
    notes = [
        n
        for r in discovery(raw, services, task_definition(("web", awslogs("/ecs/shared"))))
        for n in r["notes"]
    ]
    assert sum("one log group can belong to one target" in n for n in notes) == 2


def test_discovery_reports_a_missing_service_without_failing():
    found = discovery(draft(entry()), {}, task_definition())[0]
    assert "not found" in found["notes"][0] and "existing_log_groups" not in found


def test_discovery_output_is_pasteable():
    found = discovery(
        draft(entry()),
        {"orders-api": [service("orders-api", groups=[TG])]},
        task_definition(("web", awslogs("/ecs/orders"))),
        [(TG, [LB])],
    )
    text = ecs_discover.render(found)
    assert '"existing_log_groups": [' in text and '"load_balancer": {' in text and LB in text


def test_discovery_explains_a_target_group_with_no_load_balancer_yet():
    found = discovery(
        draft(entry()),
        {"orders-api": [service("orders-api", groups=[TG])]},
        task_definition(("web", awslogs("/ecs/orders"))),
        [(TG, [])],
    )[0]
    assert found["load_balancer"] is None and "not attached to any load balancer" in found["notes"][0]
