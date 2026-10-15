"""fetch_ecs: what the model learns about an ECS service, with AWS's own shapes behind a Stubber."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import boto3
import pytest
from botocore.stub import Stubber

from tests.helpers import ROOT, body, load_lambda
from tracaus.runtime import operation, validate
from tracaus.transport import fits

ecs_tool = load_lambda("fetch_ecs")

EC2 = "i-0123456789abcdef0"
ORDERS, WORKER = "ecs-orders", "ecs-worker"
TG = "arn:aws:elasticloadbalancing:eu-central-1:123456789012:targetgroup/orders/73e2d6bc24d8a067"
TASK_DEFINITION = "arn:aws:ecs:eu-central-1:123456789012:task-definition/orders:12"
OLD_DEFINITION = "arn:aws:ecs:eu-central-1:123456789012:task-definition/orders:11"
SCOPE = {
    ORDERS: {
        "cluster": "prod-cluster",
        "service": "orders-api",
        "cluster_arn": "arn:aws:ecs:eu-central-1:123456789012:cluster/prod-cluster",
        "container_insights": False,
        "load_balancer": {
            "LoadBalancer": "app/orders-alb/50dc6c495c0c9188",
            "TargetGroup": "targetgroup/orders/73e2d6bc24d8a067",
            "target_group_arn": TG,
        },
    },
    WORKER: {
        "cluster": "prod-cluster",
        "service": "worker",
        "cluster_arn": "arn:aws:ecs:eu-central-1:123456789012:cluster/prod-cluster",
        "container_insights": False,
        "load_balancer": None,
    },
}
NOW = datetime.now(timezone.utc).replace(microsecond=0)
ANCHOR = NOW - timedelta(minutes=10)


def at(minutes):
    return ANCHOR + timedelta(minutes=minutes)


@pytest.fixture
def scope(tmp_path, monkeypatch):
    (tmp_path / "ecs.json").write_text(json.dumps(SCOPE))
    monkeypatch.setenv("ECS_TARGETS_FILE", str(tmp_path / "ecs.json"))
    monkeypatch.setenv("ALLOWED_INSTANCE_IDS", f"{EC2},{ORDERS},{WORKER}")


def make(service):
    session = boto3.session.Session(
        aws_access_key_id="synthetic", aws_secret_access_key="synthetic", region_name="eu-central-1"
    )
    client = session.client(service)
    return client, Stubber(client)


def service(status="ACTIVE", **extra):
    return {
        "serviceName": "orders-api",
        "status": status,
        "desiredCount": 3,
        "runningCount": 1,
        "pendingCount": 0,
        "taskDefinition": TASK_DEFINITION,
        "deployments": [
            {
                "status": "PRIMARY",
                "rolloutState": "IN_PROGRESS",
                "rolloutStateReason": "ECS deployment ecs-svc/1 in progress.",
                "taskDefinition": TASK_DEFINITION,
                "createdAt": at(-3),
                "desiredCount": 3,
                "runningCount": 1,
                "pendingCount": 2,
                "failedTasks": 4,
            },
            {
                "status": "ACTIVE",
                "taskDefinition": OLD_DEFINITION,
                "createdAt": at(-60),
                "desiredCount": 0,
                "runningCount": 2,
                "pendingCount": 0,
            },
        ],
        "events": [
            {"id": "e1", "createdAt": at(-2), "message": "(service orders-api) was unable to place a task."},
            {
                "id": "e0",
                "createdAt": at(-300),
                "message": "(service orders-api) has reached a steady state.",
            },
        ],
        **extra,
    }


def stopped(
    code="EssentialContainerExited", reason="Essential container in task exited", exit_code=137, minutes=-1
):
    return {
        "taskArn": f"arn:aws:ecs:eu-central-1:123456789012:task/prod-cluster/{'a' * 32}",
        "taskDefinitionArn": TASK_DEFINITION,
        "stopCode": code,
        "stoppedReason": reason,
        "stoppedAt": at(minutes),
        "containers": [
            {
                "name": "web",
                "exitCode": exit_code,
                "reason": "OutOfMemoryError: Container killed due to memory usage",
            }
        ],
    }


def definition(**extra):
    return {
        "taskDefinition": {
            "family": "orders",
            "revision": 12,
            "cpu": "512",
            "memory": "1024",
            "containerDefinitions": [
                {
                    "name": "web",
                    "image": "123456789012.dkr.ecr.eu-central-1.amazonaws.com/orders:1.4",
                    "cpu": 0,
                    "memory": 1024,
                    "essential": True,
                    "environment": [{"name": "DB_PASSWORD", "value": "hunter2-synthetic"}],
                    "secrets": [
                        {
                            "name": "TOKEN",
                            "valueFrom": "arn:aws:secretsmanager:eu-central-1:123456789012:secret:tok",
                        }
                    ],
                    "command": ["--token", "synthetic-command-secret"],
                    "healthCheck": {"command": ["CMD-SHELL", "curl -f localhost"]},
                    "logConfiguration": {"logDriver": "awslogs", "options": {"awslogs-group": "/ecs/orders"}},
                }
            ],
            **extra,
        }
    }


def health(*targets):
    return {
        "TargetHealthDescriptions": [
            {
                "Target": {"Id": f"10.0.0.{n}", "Port": 8080},
                "TargetHealth": {
                    "State": state,
                    **({"Reason": reason} if reason else {}),
                    **({"Description": description} if description else {}),
                },
            }
            for n, (state, reason, description) in enumerate(targets, 1)
        ]
    }


def run(stubs, **params):
    params.setdefault("instance_id", ORDERS)
    params.setdefault("time_string", ANCHOR.strftime("%Y-%m-%d %H:%M:%S"))
    event = {"parameters": [{"name": k, "value": v} for k, v in params.items()]}
    clients = {name: client for name, (client, _) in stubs.items()}
    for _, stub in stubs.values():
        stub.activate()
    with patch("boto3.client", side_effect=lambda name, **kw: clients[name]):
        return body(ecs_tool.lambda_handler(event, None))


def stubs(svc=None, tasks=(), lb=None, services=None):
    ecs, ecs_stub = make("ecs")
    ecs_stub.add_response(
        "describe_services",
        {"services": [svc] if svc else services or [], "failures": []},
        {"cluster": "prod-cluster", "services": ["orders-api"]},
    )
    if svc and svc["status"] != "INACTIVE":
        arns = [t["taskArn"] for t in tasks]
        ecs_stub.add_response(
            "list_tasks",
            {"taskArns": arns},
            {
                "cluster": "prod-cluster",
                "serviceName": "orders-api",
                "desiredStatus": "STOPPED",
                "maxResults": 100,
            },
        )
        if arns:
            ecs_stub.add_response(
                "describe_tasks",
                {"tasks": list(tasks), "failures": []},
                {"cluster": "prod-cluster", "tasks": arns},
            )
        ecs_stub.add_response("describe_task_definition", definition(), {"taskDefinition": TASK_DEFINITION})
    found = {"ecs": (ecs, ecs_stub)}
    if lb is not None:
        elb, elb_stub = make("elbv2")
        elb_stub.add_response("describe_target_health", lb, {"TargetGroupArn": TG})
        found["elbv2"] = (elb, elb_stub)
    return found


def contract(result):
    schema = operation("fetch_ecs")["responses"]["200"]["content"]["application/json"]["schema"]
    validate(result, schema)  # the runtime rejects any answer that does not match, so it must


def test_the_state_of_a_failing_service(scope):
    found = stubs(
        service(),
        [stopped()],
        health(("unhealthy", "Target.FailedHealthChecks", "Health checks failed"), ("healthy", None, None)),
    )
    result = run(found)
    contract(result)
    assert (result["status"], result["complete"], result["truncated"]) == ("ok", True, False)
    assert result["instance_id"] == ORDERS and result["ecs_service"] == "prod-cluster/orders-api"
    assert result["service"]["desired_count"] == 3 and result["service"]["running_count"] == 1
    primary = result["service"]["deployments"][0]
    assert (primary["rollout_state"], primary["failed_tasks"], primary["task_definition"]) == (
        "IN_PROGRESS",
        4,
        "orders:12",
    )
    assert [e["message"] for e in result["service_events"]] == [
        "(service orders-api) was unable to place a task."
    ]
    task = result["stopped_tasks"][0]
    assert (task["stop_code"], task["task_definition"], task["task"]) == (
        "EssentialContainerExited",
        "orders:12",
        "a" * 32,
    )
    assert task["containers"] == [
        {"name": "web", "exit_code": 137, "reason": "OutOfMemoryError: Container killed due to memory usage"}
    ]
    assert result["load_balancer"]["counts"] == {"unhealthy": 1, "healthy": 1}
    assert result["load_balancer"]["targets"][0] == {
        "state": "unhealthy",
        "reason": "Target.FailedHealthChecks",
        "description": "Health checks failed",
        "port": 8080,
    }
    for _, stub in found.values():
        stub.assert_no_pending_responses()


def test_nothing_secret_leaves_the_task_definition(scope):
    text = json.dumps(run(stubs(service(), [], health())))
    for forbidden in (
        "hunter2-synthetic",
        "DB_PASSWORD",
        "secretsmanager",
        "synthetic-command-secret",
        "10.0.0.",
    ):
        assert forbidden not in text
    assert "orders:1.4" in text and '"health_check": true' in text


def test_the_task_definition_summary_is_an_allowlist(scope):
    block = run(stubs(service(), [], health()))["task_definition"]
    assert set(block) == {"family", "revision", "cpu", "memory", "containers"}
    assert set(block["containers"][0]) == {
        "name", "image", "cpu", "memory", "memory_reservation", "essential", "health_check", "log_driver",
    }  # fmt: skip
    assert block["containers"][0]["log_driver"] == "awslogs"


def test_only_the_incident_window_is_reported(scope):
    old = stopped(minutes=-120)
    result = run(stubs(service(), [stopped(minutes=-1), old], health()), window_minutes="30")
    assert len(result["stopped_tasks"]) == 1 and result["stopped_tasks"][0]["stopped_at"] == at(-1).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert len(result["service_events"]) == 1  # the steady-state message is hours old


def test_stopped_tasks_come_newest_first(scope):
    tasks = [
        stopped(minutes=-9, exit_code=1),
        stopped(minutes=-2, exit_code=2),
        stopped(minutes=-5, exit_code=3),
    ]
    for n, task in enumerate(tasks):
        task["taskArn"] = task["taskArn"][:-1] + str(n)
    result = run(stubs(service(), tasks, health()))
    assert [t["containers"][0]["exit_code"] for t in result["stopped_tasks"]] == [2, 3, 1]


def test_no_alb_means_no_elb_call_and_no_section(scope):
    ecs, stub = make("ecs")
    stub.add_response(
        "describe_services",
        {"services": [service(serviceName="worker")], "failures": []},
        {"cluster": "prod-cluster", "services": ["worker"]},
    )
    stub.add_response(
        "list_tasks",
        {"taskArns": []},
        {"cluster": "prod-cluster", "serviceName": "worker", "desiredStatus": "STOPPED", "maxResults": 100},
    )
    stub.add_response("describe_task_definition", definition(), {"taskDefinition": TASK_DEFINITION})
    result = run({"ecs": (ecs, stub)}, instance_id=WORKER)  # `elbv2` is not even available to call
    contract(result)
    assert "load_balancer" not in result and result["stopped_tasks"] == []


@pytest.mark.parametrize("status", ["INACTIVE"])
def test_a_deleted_service_is_no_data_not_an_error(scope, status):
    result = run(stubs(service(status)))
    contract(result)
    assert result["status"] == "no_data" and "deleted" in result["message"]


def test_a_missing_service_or_cluster_is_no_data(scope):
    assert run(stubs())["status"] == "no_data"
    ecs, stub = make("ecs")
    stub.add_client_error("describe_services", "ClusterNotFoundException", "gone", 400)
    assert run({"ecs": (ecs, stub)})["status"] == "no_data"


def test_an_access_failure_is_an_error_not_no_data(scope):
    ecs, stub = make("ecs")
    stub.add_client_error("describe_services", "AccessDeniedException", "no", 403)
    result = run({"ecs": (ecs, stub)})
    assert result["status"] == "error" and result["error_code"] == "ECS_ACCESS_FAILED"


def test_unreadable_target_health_makes_the_answer_incomplete_not_failed(scope):
    found = stubs(service(), [], None)
    elb, stub = make("elbv2")
    stub.add_client_error("describe_target_health", "AccessDenied", "no", 403)
    found["elbv2"] = (elb, stub)
    result = run(found)
    contract(result)
    assert result["status"] == "ok" and result["complete"] is False
    assert result["load_balancer"] == {"status": "unavailable", "counts": {}, "targets": []}


@pytest.mark.parametrize(
    "params",
    [
        {"instance_id": EC2},  # an instance, not a service
        {"instance_id": "ecs-unlisted"},
        {"instance_id": "ecs-has-hyphen"},
        {"instance_id": "orders"},
        {"time_string": (NOW + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")},
        {"time_string": "yesterday"},
    ],
)
def test_requests_outside_the_scope_never_reach_aws(scope, params):
    result = run({}, **params)
    assert result["status"] == "error" and result["error_code"] == "INVALID_REQUEST"


def test_a_target_without_packaged_scope_is_refused(scope, monkeypatch):
    monkeypatch.setenv("ALLOWED_INSTANCE_IDS", f"{EC2},{ORDERS},{WORKER},ecs-new")
    assert run({}, instance_id="ecs-new")["status"] == "error"


def test_a_huge_answer_is_trimmed_to_the_budget_and_says_so(scope):
    many = []
    for n in range(10):
        task = stopped(reason="x" * 290, minutes=-(n % 9) - 1)
        task["taskArn"] = task["taskArn"][:-2] + f"{n:02d}"
        task["containers"] = [{"name": f"c{k}", "exitCode": 1, "reason": "y" * 290} for k in range(8)]
        many.append(task)
    svc = service()
    svc["events"] = [
        {"id": f"e{n}", "createdAt": at(-1) + timedelta(seconds=n), "message": "m" * 290} for n in range(60)
    ]
    result = run(stubs(svc, many, health(*[("unhealthy", "Target.Timeout", "d" * 290)] * 30)))
    contract(result)
    assert result["truncated"] is True and result["complete"] is False and result["status"] == "partial"
    assert fits({}, result)  # the answer that reached the model is inside the transport budget


def test_long_text_is_clipped(scope):
    task = stopped(reason="r" * 5000)
    result = run(stubs(service(), [task], health()))
    assert len(result["stopped_tasks"][0]["stopped_reason"]) == 300


def test_the_schema_is_the_contract_the_runtime_enforces(scope):
    result = run(stubs(service(), [stopped()], health(("healthy", None, None))))
    result["unexpected"] = 1
    with pytest.raises(Exception):
        contract(result)
    assert (ROOT / "schemas/fetch_ecs.json").exists()


def test_a_stopped_task_without_a_stop_time_is_kept_and_one_still_running_is_not(scope):
    unknown = stopped(minutes=-1)
    del unknown["stoppedAt"]
    unknown["lastStatus"] = "STOPPED"
    stopping = stopped(minutes=-1)
    del stopping["stoppedAt"]
    stopping["lastStatus"] = "DEPROVISIONING"
    stopping["taskArn"] = stopping["taskArn"][:-1] + "z"
    result = run(stubs(service(), [unknown, stopping], health()))
    contract(result)
    assert [(t["task"], t["stopped_at"]) for t in result["stopped_tasks"]] == [("a" * 32, None)]


def many_stopped(count, minutes=-1):
    tasks = []
    for n in range(count):
        task = stopped(minutes=minutes - n % 5)
        task["taskArn"] = task["taskArn"][:-6] + f"{n:06d}"
        tasks.append(task)
    return tasks


def paged_stubs(tasks, page_sizes, more=False):
    """list_tasks answers in pages of the given sizes (the last one offers another page when `more`)."""
    ecs, stub = make("ecs")
    stub.add_response(
        "describe_services",
        {"services": [service()], "failures": []},
        {"cluster": "prod-cluster", "services": ["orders-api"]},
    )
    first = 0
    for page, size in enumerate(page_sizes):
        arns = [t["taskArn"] for t in tasks[first : first + size]]
        first += size
        last = page == len(page_sizes) - 1
        stub.add_response(
            "list_tasks",
            {"taskArns": arns, **({"nextToken": f"page{page + 1}"} if not last or more else {})},
            {
                "cluster": "prod-cluster",
                "serviceName": "orders-api",
                "desiredStatus": "STOPPED",
                "maxResults": 100,
                **({"nextToken": f"page{page}"} if page else {}),
            },
        )
    for start in range(0, len(tasks), 100):
        batch = tasks[start : start + 100]
        stub.add_response(
            "describe_tasks",
            {"tasks": batch, "failures": []},
            {"cluster": "prod-cluster", "tasks": [t["taskArn"] for t in batch]},
        )
    stub.add_response("describe_task_definition", definition(), {"taskDefinition": TASK_DEFINITION})
    elb, elb_stub = make("elbv2")
    elb_stub.add_response("describe_target_health", health(), {"TargetGroupArn": TG})
    return {"ecs": (ecs, stub), "elbv2": (elb, elb_stub)}


def test_every_page_of_stopped_tasks_is_read_and_the_cut_is_declared(scope):
    tasks = many_stopped(250)
    found = paged_stubs(tasks, [100, 100, 50])
    result = run(found)
    found["ecs"][1].assert_no_pending_responses()  # three list_tasks calls and three describe_tasks batches
    contract(result)
    assert len(result["stopped_tasks"]) == 10
    assert result["truncated"] is True and result["complete"] is False and result["status"] == "partial"


def test_a_page_limit_hit_is_declared_even_when_nothing_in_the_window_was_cut(scope):
    old = many_stopped(500, minutes=-120)  # all outside the window: no row is dropped by a cap
    result = run(paged_stubs(old, [100] * 5, more=True))
    assert result["stopped_tasks"] == []
    assert result["truncated"] is True and result["complete"] is False and result["status"] == "partial"


def test_an_answer_that_lost_nothing_stays_complete(scope):
    result = run(paged_stubs(many_stopped(3), [3]))
    assert result["status"] == "ok" and result["complete"] is True and result["truncated"] is False


def test_every_cap_that_drops_a_row_is_noted():
    notes = []
    assert ecs_tool.cap(list(range(5)), 5, notes) == [0, 1, 2, 3, 4] and not notes
    assert ecs_tool.cap(list(range(6)), 5, notes) == [0, 1, 2, 3, 4] and notes == [5]
    deployments = [
        {"createdAt": at(-n), "status": "ACTIVE", "taskDefinition": TASK_DEFINITION} for n in range(7)
    ]
    notes.clear()
    ecs_tool.service_block(service(deployments=deployments), notes)
    assert notes == [ecs_tool.MAX_DEPLOYMENTS]
    notes.clear()
    events = [{"createdAt": at(-1) + timedelta(seconds=n), "message": "m"} for n in range(25)]
    kept = ecs_tool.service_events(service(events=events), at(-5), at(0), notes)
    assert len(kept) == ecs_tool.MAX_EVENTS and notes == [ecs_tool.MAX_EVENTS]
    assert kept[-1]["at"] == (at(-1) + timedelta(seconds=24)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )  # the newest are kept
