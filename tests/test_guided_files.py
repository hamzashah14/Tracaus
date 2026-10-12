"""The three settings files built from choices, written safely, and the readiness report."""

import copy
import json
import os
import stat

import pytest

from infra.guided.files import FilesError, build, ecs_id, readiness, validate, write

ACCOUNT, REGION = "123456789012", "eu-central-1"
ROLES = {
    "operator": f"arn:aws:iam::{ACCOUNT}:role/tracaus-staging-operator",
    "execution": f"arn:aws:iam::{ACCOUNT}:role/tracaus-staging-cfn-execution",
    "ui": f"arn:aws:iam::{ACCOUNT}:role/tracaus-staging-ui",
}
LB = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/orders-alb/50dc6c495c0c9188"
TG = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/orders/73e2d6bc24d8a067"
EC2 = {
    "id": "i-0123456789abcdef0",
    "label": "web-1",
    "disk_path": "/",
    "process_exe": "nginx",
    "nginx": True,
    "existing_log_groups": ["/app/web"],
    "log_groups": ["application"],
    "log_files": [{"group": "application", "file": "/var/log/app/*.log"}],
}
ECS_ALB = {
    "id": "ecs-orders",
    "cluster": "prod",
    "service": "orders-api",
    "label": "orders-api",
    "load_balancer": {"load_balancer_arn": LB, "target_group_arn": TG},
    "log_groups": ["/ecs/orders"],
    "container_insights": True,
}
ECS_PLAIN = {**ECS_ALB, "id": "ecs-worker", "service": "worker", "load_balancer": None, "log_groups": []}
MODEL_API = {
    "provider": "model_api",
    "model_id": "model-x",
    "protocol": "openai",
    "base_url": "https://api.example.com/v1",
}


def choices(**change):
    value = {
        "project": "tracaus",
        "environment": "staging",
        "account_id": ACCOUNT,
        "region": REGION,
        "bedrock_region": REGION,
        "release_id": "r001",
        "emails": ["ops@example.com"],
        "model": {
            "provider": "bedrock",
            "model_id": "example.model-v1",
            "model_arns": [f"arn:aws:bedrock:{REGION}::foundation-model/example.model-v1"],
        },
        "roles": ROLES,
        "profile": "tracaus-operator",
        "status_base_url": "https://localhost:8501/",
        "ec2": [EC2],
        "ecs": [],
    }
    value.update(change)
    return copy.deepcopy(value)


@pytest.mark.parametrize(
    "service,taken,expected",
    [
        ("orders-api", set(), "ecs-orders_api"),
        ("Orders.API!", set(), "ecs-ordersapi"),
        ("a" * 40, set(), "ecs-" + "a" * 24),
        ("orders", {"ecs-orders"}, "ecs-orders_2"),
        ("x" * 30, {"ecs-" + "x" * 24}, "ecs-" + "x" * 22 + "_2"),
        ("!!!", set(), "ecs-service"),
    ],
)
def test_ecs_ids(service, taken, expected):
    assert ecs_id(service, taken) == expected


@pytest.mark.parametrize(
    "case",
    [
        choices(),
        choices(ec2=[], ecs=[ECS_ALB, ECS_PLAIN]),
        choices(ecs=[ECS_ALB]),
        choices(ec2=[], ecs=[ECS_PLAIN], model=MODEL_API),
    ],
    ids=["ec2", "ecs", "mixed", "model-api"],
)
def test_built_files_pass_the_loaders(tmp_path, case):
    write(tmp_path, build(case))
    spec = validate(tmp_path)
    assert spec["account_id"] == ACCOUNT and spec["reference_only"] is False
    assert not (tmp_path / ".staging").exists()


def test_ec2_entry_details():
    deployment = build(choices())["deployment"]
    entry = deployment["instances"][0]
    assert entry["nginx_alarm"] is True and "nginx_filters" in deployment
    assert entry["existing_log_groups"] == [{"name": "/app/web", "streams": "instance"}]
    assert entry["log_files"] == [{"group": "application", "file": "/var/log/app/*.log"}]


def test_no_nginx_means_no_filters():
    value = choices(ec2=[{**EC2, "nginx": False}])
    assert "nginx_filters" not in build(value)["deployment"]


def test_ecs_entries_keep_the_alb_answer():
    deployment = build(choices(ec2=[], ecs=[ECS_ALB, ECS_PLAIN]))["deployment"]
    assert deployment["instances"] == []
    orders, worker = deployment["ecs_services"]
    assert orders["load_balancer"]["target_group_arn"] == TG and orders["container_insights"] is True
    assert worker["load_balancer"] is None and "existing_log_groups" not in worker


def test_runtime_and_automation_defaults():
    built = build(choices())
    assert built["runtime"]["status_base_url"] == "https://localhost:8501/"
    assert built["runtime"]["investigation_paused"] is True and "fallback_email" not in built["runtime"]
    assert built["automation"]["profile"] == "tracaus-operator"
    assert built["automation"]["spec"] == "deployment.json"


def test_target_limits():
    with pytest.raises(FilesError, match="at least one"):
        build(choices(ec2=[], ecs=[]))
    many = [{**ECS_PLAIN, "id": f"ecs-s{n}", "service": f"s{n}"} for n in range(11)]
    with pytest.raises(FilesError, match="at most 10"):
        build(choices(ec2=[], ecs=many))


def test_files_are_private_and_the_previous_copy_is_kept(tmp_path):
    directory = tmp_path / "work"
    write(directory, build(choices()))
    for name in ("deployment.json", "runtime.json", "automation.json"):
        assert stat.S_IMODE(os.stat(directory / name).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    first = json.loads((directory / "deployment.json").read_text())
    write(directory, build(choices(release_id="r002")))
    assert json.loads((directory / "deployment.json.previous").read_text()) == first
    assert json.loads((directory / "deployment.json").read_text())["release_id"] == "r002"


def test_an_invalid_file_set_leaves_nothing_behind(tmp_path):
    bad = choices(ec2=[{**EC2, "disk_path": "no-slash"}])
    with pytest.raises(FilesError, match="do not validate"):
        write(tmp_path, build(bad))
    assert not (tmp_path / "deployment.json").exists() and not (tmp_path / ".staging").exists()
    write(tmp_path, build(choices()))
    before = (tmp_path / "deployment.json").read_text()
    with pytest.raises(FilesError):
        write(tmp_path, build(bad))
    assert (tmp_path / "deployment.json").read_text() == before


def test_a_started_deployment_is_never_touched(tmp_path):
    write(tmp_path, build(choices()))
    (tmp_path / "state.json").write_text("{}")
    with pytest.raises(FilesError, match="refresh"):
        write(tmp_path, build(choices(release_id="r002")))


def test_a_symlinked_settings_file_is_refused(tmp_path):
    write(tmp_path, build(choices()))
    (tmp_path / "deployment.json").unlink()
    (tmp_path / "elsewhere.json").write_text("{}")
    (tmp_path / "deployment.json").symlink_to(tmp_path / "elsewhere.json")
    with pytest.raises(FilesError, match="symlink"):
        write(tmp_path, build(choices()))


def test_validation_messages_do_not_echo_values(tmp_path):
    bad = choices(emails=["not-an-email-SECRET123"])
    with pytest.raises(FilesError) as error:
        write(tmp_path, build(bad))
    assert "SECRET123" not in str(error.value)


def agent(**extra):
    return {
        "state": "found",
        "any": True,
        "memory": True,
        "disk_paths": ["/"],
        "disk_wide": [],
        "exes": ["nginx"],
        **extra,
    }


def states(entry):
    return {c["name"]: c["state"] for c in entry["checks"]}


def test_readiness_for_a_healthy_ec2_instance():
    report = readiness(choices(), {"agent": {EC2["id"]: agent()}, "groups": {"/app/web": True}})
    result = states(report[0])
    assert result["memory metric"] == result["disk metric (/)"] == result["process metric (nginx)"] == "found"
    assert result["log group /app/web"] == "found"
    assert result["log group application"] == "unknown" and result["nginx logs"] == "unknown"


def test_readiness_names_the_action_for_each_gap():
    gap = {"state": "missing", "any": False, "memory": False, "disk_paths": [], "disk_wide": [], "exes": []}
    report = readiness(choices(), {"agent": {EC2["id"]: gap}, "groups": {"/app/web": False}})
    actions = {c["name"]: c["action"] for c in report[0]["checks"]}
    assert "collector-examples/i-0123456789abcdef0.json" in actions["memory metric"]
    assert states(report[0])["log group /app/web"] == "missing"


def test_readiness_flags_a_disk_metric_with_extra_dimensions():
    wide = agent(disk_paths=[], disk_wide=["/"])
    report = readiness(choices(), {"agent": {EC2["id"]: wide}})
    disk = next(c for c in report[0]["checks"] if c["name"].startswith("disk"))
    assert disk["state"] == "missing" and "aggregation_dimensions" in disk["action"]


def test_readiness_denied_reads_are_unknown_not_missing():
    report = readiness(choices(), {"agent": {EC2["id"]: {"state": "unknown", "reason": "AccessDenied"}}})
    result = states(report[0])
    assert result["memory metric"] == "unknown" and result["disk metric (/)"] == "unknown"


def test_readiness_for_ecs():
    report = readiness(choices(ec2=[], ecs=[ECS_ALB, ECS_PLAIN]), {"groups": {"/ecs/orders": True}})
    orders, worker = report
    assert states(orders)["log group /ecs/orders"] == "found" and states(orders)["load balancer"] == "found"
    assert states(worker)["log groups"] == "missing"
    assert "off" in next(c for c in worker["checks"] if c["name"] == "load balancer")["action"]
