"""task_failure and ECS resource pressure: each rejection is caused by the rule it targets, not by accident."""

import json

import pytest

from tracaus import diagnosis

T0, T1 = "2026-10-01T10:00:00Z", "2026-10-01T10:15:00Z"
SVC = "ecs-orders"


def stopped(code="EssentialContainerExited", reason="Essential container in task exited", exit_code=137):
    return {
        "task": "a" * 32,
        "stop_code": code,
        "stopped_reason": reason,
        "stopped_at": "2026-10-01T10:05:00Z",
        "task_definition": "orders:12",
        "containers": [{"name": "web", "exit_code": exit_code, "reason": None}],
    }


def ecs_result(*tasks, **changes):
    return {
        "status": "ok",
        "complete": True,
        "truncated": False,
        "instance_id": SVC,
        "ecs_service": "prod-cluster/orders-api",
        "window_start": T0,
        "window_end": T1,
        "service": {
            "status": "ACTIVE",
            "desired_count": 3,
            "running_count": 1,
            "pending_count": 0,
            "task_definition": "orders:12",
            "deployments": [],
        },
        "service_events": [],
        "stopped_tasks": list(tasks),
        **changes,
    }


def metric_result(name="MemoryUtilization", value=96, namespace="AWS/ECS"):
    descriptor = {
        "namespace": namespace,
        "metric_name": name,
        "statistic": "Average",
        "instance_id": SVC,
        "metric_id": "builtin",
        "dimensions": [{"Name": "ClusterName", "Value": "prod-cluster"}],
    }
    return {
        "status": "ok",
        "complete": True,
        "instance_id": SVC,
        "window_start": T0,
        "window_end": T1,
        "descriptor": descriptor,
        "datapoints": [{"timestamp": "2026-10-01T10:05:00Z", "value": value}],
        "metric": name,
        "namespace": namespace,
        "statistic": "Average",
        "period_seconds": 60,
    }


def report(kind, evidence, facts):
    """Validate a candidate that cites `evidence` ([(tool, result)]) with `facts` ([(index, path, quote)])."""
    catalog = [diagnosis.catalog_entry(tool, result, n) for n, (tool, result) in enumerate(evidence)]
    ids = [e["id"] for e in catalog]
    candidate = {
        "version": 1,
        "findings": [{"kind": kind, "statement": "A qualified symptom.", "evidence_ids": ids}],
        "facts": [{"evidence_id": ids[i], "path": path, "quote": quote} for i, path, quote in facts],
        "hypotheses": [],
        "limitations": ["Synthetic."],
        "recommendations": ["Operator review."],
    }
    return diagnosis.validate(json.dumps(candidate), catalog)["status"]


CRASH = [("fetch_ecs", ecs_result(stopped()))]
QUOTE = [(0, "/stopped_tasks/0/stop_code", "EssentialContainerExited")]


def test_a_crash_cited_with_its_stop_code_is_a_valid_task_failure():
    assert report("task_failure", CRASH, QUOTE) == "VALID"
    assert (
        report("task_failure", CRASH, QUOTE + [(0, "/stopped_tasks/0/containers/0/exit_code", "137")])
        == "VALID"
    )


@pytest.mark.parametrize(
    ("code", "reason", "valid"),
    [
        ("EssentialContainerExited", "x", True),
        ("TaskFailedToStart", "CannotPullContainerError", True),
        ("SpotInterruption", "x", True),
        ("UserInitiated", "Task stopped by user", False),
        ("ServiceSchedulerInitiated", "Scaling activity initiated by (deployment ecs-svc/1)", False),
        ("ServiceSchedulerInitiated", "Task failed ELB health checks in (target-group x)", True),
    ],
)
def test_routine_stops_are_not_task_failures(code, reason, valid):
    evidence = [("fetch_ecs", ecs_result(stopped(code, reason)))]
    quote = [(0, "/stopped_tasks/0/stop_code", code)]
    assert (report("task_failure", evidence, quote) == "VALID") is valid


def test_the_failure_must_come_from_service_state_not_from_a_metric():
    metrics = [("fetch_metrics", metric_result())]
    assert report("task_failure", metrics, [(0, "/datapoints/0/value", "96")]) == "REJECTED"


def test_the_finding_must_quote_the_stopped_task():
    assert report("task_failure", CRASH, [(0, "/service/desired_count", "3")]) == "REJECTED"
    assert report("task_failure", CRASH, [(0, "/ecs_service", "prod-cluster/orders-api")]) == "REJECTED"


def test_incomplete_or_empty_service_state_is_not_evidence_of_a_failure():
    assert (
        report(
            "task_failure", [("fetch_ecs", ecs_result(stopped(), complete=False, status="partial"))], QUOTE
        )
        == "REJECTED"
    )
    assert (
        report("task_failure", [("fetch_ecs", ecs_result())], [(0, "/service/desired_count", "3")])
        == "REJECTED"
    )


def test_a_quote_that_does_not_match_the_result_is_rejected():
    assert (
        report("task_failure", CRASH, [(0, "/stopped_tasks/0/stop_code", "TaskFailedToStart")]) == "REJECTED"
    )


def test_ecs_services_have_no_health_probes_so_hang_and_service_failure_never_apply():
    assert report("hang_correlated", CRASH, QUOTE) == "REJECTED"
    assert report("service_failure", CRASH, QUOTE) == "REJECTED"


def test_ecs_memory_and_cpu_pressure_count_as_resource_pressure():
    pair = [("fetch_metrics", metric_result("MemoryUtilization", 96)), ("fetch_ecs", ecs_result(stopped()))]
    facts = [(0, "/datapoints/0/value", "96"), (1, "/service/running_count", "1")]
    assert report("resource_pressure", pair, facts) == "VALID"
    cpu = [("fetch_metrics", metric_result("CPUUtilization", 91)), pair[1]]
    assert report("resource_pressure", cpu, [(0, "/datapoints/0/value", "91"), facts[1]]) == "VALID"
    low = [("fetch_metrics", metric_result("MemoryUtilization", 40)), pair[1]]
    assert report("resource_pressure", low, [(0, "/datapoints/0/value", "40"), facts[1]]) == "REJECTED"


def test_ecs_pressure_names_do_not_pass_for_the_wrong_namespace():
    wrong = [
        ("fetch_metrics", metric_result("MemoryUtilization", 96, namespace="Custom/App")),
        ("fetch_ecs", ecs_result(stopped())),
    ]
    assert (
        report(
            "resource_pressure", wrong, [(0, "/datapoints/0/value", "96"), (1, "/service/running_count", "1")]
        )
        == "REJECTED"
    )


def test_the_evidence_source_of_a_service_is_its_cluster_and_name():
    entry = diagnosis.catalog_entry("fetch_ecs", ecs_result(stopped()), 0)
    assert entry["source"] == "prod-cluster/orders-api" and entry["instance_id"] == SVC
    metrics = diagnosis.catalog_entry("fetch_metrics", metric_result(), 0)
    assert metrics["source"] == metric_result()["descriptor"]  # EC2 and ECS metrics are unchanged


def test_the_model_is_told_about_the_new_kind_and_its_limits():
    assert "task_failure" in diagnosis.INSTRUCTION and "never apply to them" in diagnosis.INSTRUCTION
    from tests.helpers import ROOT

    text = (ROOT / "agent-instruction.txt").read_text()
    assert "ECS SERVICES" in text and "fetch_ecs" in text and "ServiceSchedulerInitiated" in text


def test_the_finding_must_quote_the_task_that_failed_not_a_routine_one_beside_it():
    routine = stopped(
        "ServiceSchedulerInitiated", "Scaling activity initiated by (deployment ecs-svc/1)", None
    )
    both = [("fetch_ecs", ecs_result(routine, stopped()))]  # index 0 is routine, index 1 crashed
    assert (
        report("task_failure", both, [(0, "/stopped_tasks/0/stop_code", "ServiceSchedulerInitiated")])
        == "REJECTED"
    )
    assert (
        report("task_failure", both, [(0, "/stopped_tasks/1/stop_code", "EssentialContainerExited")])
        == "VALID"
    )
    both_quoted = [
        (0, "/stopped_tasks/0/stop_code", "ServiceSchedulerInitiated"),
        (0, "/stopped_tasks/1/stop_code", "EssentialContainerExited"),
    ]
    assert report("task_failure", both, both_quoted) == "VALID"  # quoting the routine one as context is fine


def test_a_citation_past_the_end_of_the_stopped_tasks_does_not_count():
    assert (
        report("task_failure", CRASH, [(0, "/stopped_tasks/5/stop_code", "EssentialContainerExited")])
        == "REJECTED"
    )
