"""Read-only discovery. Every client is a real botocore client behind a Stubber, so each call and response
is validated against AWS's own service model."""

import pytest
from botocore.exceptions import EndpointConnectionError, NoCredentialsError

from infra.guided import discover
from infra.guided.discover import DiscoveryError
from tests.guided_fakes import ACCOUNT, REGION, Fake

ID1, ID2 = "i-0123456789abcdef0", "i-0fedcba9876543210"
TG = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/orders/73e2d6bc24d8a067"
ALB = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/orders-alb/50dc6c495c0c9188"
NLB = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/net/orders/bbbbbbbbbbbbbbbb"


@pytest.fixture
def fake():
    return Fake()


def instance(ident, name="", state="running", **extra):
    item = {"InstanceId": ident, "State": {"Name": state}, **extra}
    if name:
        item["Tags"] = [{"Key": "Name", "Value": name}]
    return item


def test_ec2_lists_running_and_stopped_and_marks_windows_unsupported(fake):
    fake.on(
        "ec2",
        "describe_instances",
        {
            "Reservations": [
                {
                    "Instances": [
                        instance(ID2, "web-2", Platform="windows"),
                        instance(
                            ID1,
                            "web-1",
                            "stopped",
                            IamInstanceProfile={"Arn": "arn:aws:iam::1:instance-profile/p"},
                        ),
                        instance("i-not-valid", "bad"),
                    ]
                }
            ]
        },
        {"Filters": [{"Name": "instance-state-name", "Values": ["running", "stopped"]}]},
    )
    result = discover.ec2_instances(fake, REGION)
    assert result["state"] == "found" and result["skipped"] == 1
    assert [i["id"] for i in result["items"]] == [ID1, ID2]
    assert result["items"][0]["state"] == "stopped" and result["items"][0]["supported"]
    assert result["items"][1]["platform"] == "windows" and not result["items"][1]["supported"]
    fake.done()


def test_ec2_denied_is_unknown_not_empty(fake):
    fake.denied("ec2", "describe_instances", "UnauthorizedOperation")
    result = discover.ec2_instances(fake, REGION)
    assert result["state"] == "unknown" and result["reason"] == "UnauthorizedOperation"
    assert result["items"] == []


def metric(name, **dims):
    return {
        "Namespace": "CWAgent",
        "MetricName": name,
        "Dimensions": [{"Name": k, "Value": v} for k, v in {"InstanceId": ID1, **dims}.items()],
    }


def test_cwagent_facts_use_exact_dimension_sets(fake):
    fake.on(
        "cloudwatch",
        "list_metrics",
        {
            "Metrics": [
                metric("mem_used_percent"),
                metric("disk_used_percent", path="/"),
                metric("disk_used_percent", path="/data", device="xvdb", fstype="ext4"),
                metric("procstat_lookup_pid_count", exe="nginx", pid_finder="native"),
                metric("procstat_lookup_pid_count", exe="old"),
            ]
        },
        {
            "Namespace": "CWAgent",
            "Dimensions": [{"Name": "InstanceId", "Value": ID1}],
            "RecentlyActive": "PT3H",
        },
    )
    facts = discover.cwagent_facts(fake, REGION, ID1)
    assert facts["state"] == "found" and facts["memory"]
    assert facts["disk_paths"] == ["/"] and facts["disk_wide"] == ["/data"]
    assert facts["exes"] == ["nginx"]
    fake.done()


def test_cwagent_with_no_recent_metrics_is_missing(fake):
    fake.on("cloudwatch", "list_metrics", {"Metrics": []})
    facts = discover.cwagent_facts(fake, REGION, ID1)
    assert facts["state"] == "missing" and not facts["any"]


def test_cwagent_denied_is_unknown(fake):
    fake.denied("cloudwatch", "list_metrics")
    assert discover.cwagent_facts(fake, REGION, ID1)["state"] == "unknown"


def test_log_groups_with_a_stream_named_after_the_instance(fake):
    fake.on(
        "logs", "describe_log_groups", {"logGroups": [{"logGroupName": "/app/a"}, {"logGroupName": "/app/b"}]}
    )
    fake.on("logs", "describe_log_streams", {"logStreams": [{"logStreamName": ID1}]})
    fake.on("logs", "describe_log_streams", {"logStreams": []})
    result = discover.log_group_candidates(fake, REGION, [ID1])
    assert result["state"] == "found" and result["groups"] == {ID1: ["/app/a"]} and not result["capped"]
    fake.done()


def test_log_group_scan_is_bounded(fake):
    groups = [{"logGroupName": f"/g/{n}"} for n in range(5)]
    fake.on("logs", "describe_log_groups", {"logGroups": groups})
    for _ in range(2):
        fake.on("logs", "describe_log_streams", {"logStreams": []})
    result = discover.log_group_candidates(fake, REGION, [ID1], group_limit=10, call_limit=2)
    assert result["capped"] is True
    fake.done()
    fake2 = Fake()
    fake2.on("logs", "describe_log_groups", {"logGroups": groups})
    for _ in range(2):
        fake2.on("logs", "describe_log_streams", {"logStreams": []})
    assert discover.log_group_candidates(fake2, REGION, [ID1], group_limit=2)["capped"] is True


def test_log_groups_denied_is_unknown(fake):
    fake.denied("logs", "describe_log_groups")
    assert discover.log_group_candidates(fake, REGION, [ID1])["state"] == "unknown"


def ecs_cluster(fake, name="prod", insights="enabled"):
    fake.on("ecs", "list_clusters", {"clusterArns": [f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/{name}"]})
    fake.on(
        "ecs",
        "describe_clusters",
        {"clusters": [{"clusterName": name, "settings": [{"name": "containerInsights", "value": insights}]}]},
    )


def ecs_service(fake, name="orders-api", status="ACTIVE", balancers=(TG,), definition="orders:1"):
    fake.on("ecs", "list_services", {"serviceArns": [f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/prod/{name}"]})
    fake.on(
        "ecs",
        "describe_services",
        {
            "services": [
                {
                    "serviceName": name,
                    "status": status,
                    "taskDefinition": definition,
                    "launchType": "FARGATE",
                    "loadBalancers": [{"targetGroupArn": arn} for arn in balancers],
                }
            ]
        },
    )


def task_definition(fake, driver="awslogs", group="/ecs/orders"):
    log = {"logDriver": driver}
    if driver == "awslogs":
        log["options"] = {"awslogs-group": group, "awslogs-region": REGION, "awslogs-stream-prefix": "ecs"}
    fake.on(
        "ecs",
        "describe_task_definition",
        {"taskDefinition": {"containerDefinitions": [{"name": "app", "logConfiguration": log}]}},
    )


def test_ecs_service_behind_an_alb_with_awslogs(fake):
    ecs_cluster(fake)
    ecs_service(fake)
    task_definition(fake)
    fake.on(
        "elbv2",
        "describe_target_groups",
        {"TargetGroups": [{"TargetGroupArn": TG, "LoadBalancerArns": [ALB]}]},
    )
    result = discover.ecs_services(fake, REGION)
    item = result["items"][0]
    assert result["state"] == "found" and item["cluster"] == "prod" and item["service"] == "orders-api"
    assert item["load_balancer"] == "alb" and item["alb"] == {
        "load_balancer_arn": ALB,
        "target_group_arn": TG,
    }
    assert item["log_groups"] == ["/ecs/orders"] and item["container_insights"] is True
    assert item["launch_type"] == "FARGATE"
    fake.done()


def test_ecs_service_without_a_load_balancer(fake):
    ecs_cluster(fake, insights="disabled")
    ecs_service(fake, balancers=())
    task_definition(fake)
    item = discover.ecs_services(fake, REGION)["items"][0]
    assert item["load_balancer"] == "none" and item["alb"] is None and item["container_insights"] is False
    fake.done()


def test_ecs_network_load_balancer_is_flagged_unsupported(fake):
    ecs_cluster(fake)
    ecs_service(fake)
    task_definition(fake)
    fake.on(
        "elbv2",
        "describe_target_groups",
        {"TargetGroups": [{"TargetGroupArn": TG, "LoadBalancerArns": [NLB]}]},
    )
    item = discover.ecs_services(fake, REGION)["items"][0]
    assert item["load_balancer"] == "other" and any("ALB only" in n for n in item["notes"])


def test_ecs_target_group_not_attached_yet(fake):
    ecs_cluster(fake)
    ecs_service(fake)
    task_definition(fake)
    fake.on("elbv2", "describe_target_groups", {"TargetGroups": [{"TargetGroupArn": TG}]})
    item = discover.ecs_services(fake, REGION)["items"][0]
    assert item["load_balancer"] == "unknown" and any("not attached" in n for n in item["notes"])


def test_ecs_load_balancer_read_denied_keeps_the_service(fake):
    ecs_cluster(fake)
    ecs_service(fake)
    task_definition(fake)
    fake.denied("elbv2", "describe_target_groups")
    item = discover.ecs_services(fake, REGION)["items"][0]
    assert item["load_balancer"] == "unknown" and any("AccessDeniedException" in n for n in item["notes"])


def test_ecs_other_log_driver_is_reported(fake):
    ecs_cluster(fake)
    ecs_service(fake, balancers=())
    task_definition(fake, driver="splunk")
    item = discover.ecs_services(fake, REGION)["items"][0]
    assert item["log_groups"] == [] and item["other_log_drivers"] == ["splunk"]


def test_ecs_inactive_services_are_ignored(fake):
    ecs_cluster(fake)
    ecs_service(fake, status="DRAINING")
    assert discover.ecs_services(fake, REGION)["items"] == []
    fake.done()


def test_ecs_with_no_clusters(fake):
    fake.on("ecs", "list_clusters", {"clusterArns": []})
    result = discover.ecs_services(fake, REGION)
    assert result == {"state": "found", "items": [], "capped": False}


def test_ecs_denied_is_unknown(fake):
    fake.denied("ecs", "list_clusters")
    result = discover.ecs_services(fake, REGION)
    assert result["state"] == "unknown" and result["items"] == []


def profile(ident="eu.anthropic.model-v1:0", status="ACTIVE", models=2):
    return {
        "inferenceProfileName": "Model",
        "inferenceProfileArn": f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/{ident}",
        "models": [{"modelArn": f"arn:aws:bedrock:eu-west-1::foundation-model/m{n}"} for n in range(models)],
        "inferenceProfileId": ident,
        "status": status,
        "type": "SYSTEM_DEFINED",
    }


def test_bedrock_profiles_skip_inactive_and_oversized(fake):
    fake.on(
        "bedrock",
        "list_inference_profiles",
        {"inferenceProfileSummaries": [profile(), profile("old", "INACTIVE"), profile("big", models=10)]},
        {"typeEquals": "SYSTEM_DEFINED"},
    )
    result = discover.bedrock_profiles(fake, REGION)
    assert [i["id"] for i in result["items"]] == ["eu.anthropic.model-v1:0"]
    assert len(result["items"][0]["arns"]) == 3


def test_resolve_model_prefers_a_profile_then_a_foundation_model(fake):
    fake.on("bedrock", "get_inference_profile", {**profile(), "inferenceProfileId": "p"})
    found = discover.resolve_model(fake, REGION, "p")
    assert found["kind"] == "profile" and len(found["arns"]) == 3
    fake.denied("bedrock", "get_inference_profile", "ValidationException")
    fake.on(
        "bedrock",
        "get_foundation_model",
        {"modelDetails": {"modelArn": "arn:aws:bedrock:eu-central-1::foundation-model/x", "modelId": "x"}},
    )
    assert discover.resolve_model(fake, REGION, "x") == {
        "state": "found",
        "kind": "foundation",
        "arns": ["arn:aws:bedrock:eu-central-1::foundation-model/x"],
    }
    fake.denied("bedrock", "get_inference_profile", "ValidationException")
    fake.denied("bedrock", "get_foundation_model", "ValidationException")
    assert discover.resolve_model(fake, REGION, "nope")["state"] == "missing"
    fake.denied("bedrock", "get_inference_profile", "AccessDeniedException")
    assert discover.resolve_model(fake, REGION, "z")["state"] == "unknown"
    fake.done()


def role(arn):
    return {
        "Role": {
            "Path": "/",
            "RoleName": arn.rsplit("/", 1)[-1],
            "RoleId": "AROAEXAMPLE1234567",
            "Arn": arn,
            "CreateDate": "2026-01-01T00:00:00Z",
        }
    }


def test_role_state(fake):
    arn = f"arn:aws:iam::{ACCOUNT}:role/tracaus-staging-operator"
    fake.on("iam", "get_role", role(arn))
    assert discover.role_state(fake, arn)["state"] == "found"
    fake.on("iam", "get_role", role(f"arn:aws:iam::{ACCOUNT}:role/other-path/tracaus-staging-operator"))
    assert discover.role_state(fake, arn)["state"] == "conflict"
    fake.denied("iam", "get_role", "NoSuchEntity")
    assert discover.role_state(fake, arn)["state"] == "missing"
    fake.denied("iam", "get_role", "AccessDenied")
    assert discover.role_state(fake, arn)["state"] == "unknown"
    fake.done()


@pytest.mark.parametrize(
    "arn,kind,principal",
    [
        (f"arn:aws:iam::{ACCOUNT}:root", "root", None),
        (f"arn:aws:iam::{ACCOUNT}:user/ops/alice", "user", f"arn:aws:iam::{ACCOUNT}:user/ops/alice"),
        (f"arn:aws:sts::{ACCOUNT}:federated-user/bob", "other", None),
    ],
)
def test_caller_without_a_role_lookup(fake, arn, kind, principal):
    fake.on("sts", "get_caller_identity", {"UserId": "U", "Account": ACCOUNT, "Arn": arn})
    result = discover.caller(fake, REGION)
    assert (result["account"], result["kind"], result["principal_arn"]) == (ACCOUNT, kind, principal)


def test_caller_role_session_resolves_the_real_role_arn_with_its_path(fake):
    session = f"arn:aws:sts::{ACCOUNT}:assumed-role/AWSReservedSSO_Admin_abc/alice"
    real = f"arn:aws:iam::{ACCOUNT}:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_Admin_abc"
    fake.on("sts", "get_caller_identity", {"UserId": "U", "Account": ACCOUNT, "Arn": session})
    fake.on("iam", "get_role", role(real))
    assert discover.caller(fake, REGION)["principal_arn"] == real
    fake.on("sts", "get_caller_identity", {"UserId": "U", "Account": ACCOUNT, "Arn": session})
    fake.denied("iam", "get_role")
    assert discover.caller(fake, REGION)["principal_arn"] is None


@pytest.mark.parametrize("error", [EndpointConnectionError(endpoint_url="https://x"), NoCredentialsError()])
def test_transport_problems_raise_discovery_error_without_details(error):
    def raising(service, region):
        class Boom:
            def get_caller_identity(self):
                raise error

        return Boom()

    with pytest.raises(DiscoveryError) as caught:
        discover.caller(raising, REGION)
    assert "https://x" not in str(caught.value)


def test_log_groups_exist_is_exact_and_tolerates_denial(fake):
    fake.on(
        "logs",
        "describe_log_groups",
        {"logGroups": [{"logGroupName": "/ecs/orders"}, {"logGroupName": "/ecs/orders-2"}]},
    )
    fake.on("logs", "describe_log_groups", {"logGroups": [{"logGroupName": "/ecs/other-2"}]})
    fake.denied("logs", "describe_log_groups")
    result = discover.log_groups_exist(fake, REGION, ["/ecs/orders", "/ecs/other", "/ecs/x", "bad name"])
    assert result == {"/ecs/orders": True, "/ecs/other": False, "/ecs/x": None, "bad name": None}
    fake.done()
