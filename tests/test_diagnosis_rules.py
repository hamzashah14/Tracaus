"""The causal gates judge a metric where the fault was, not across every point of a wide window."""

import copy
import json

import pytest

from scripts.dev import evaluate_diagnostics as ev
from tracaus import diagnosis, safety

CASES = {case["id"]: case for case in ev.suite()[0]["cases"]}


def points(*values, first=1):
    """One datapoint a minute from 10:0<first> UTC on 2026-10-01; the evidence window is 10:00 to 10:15."""
    return [{"timestamp": f"2026-10-01T10:{first + i:02d}:00Z", "value": v} for i, v in enumerate(values)]


def verdict(case_id, **series):
    """VALID or REJECTED after replacing the datapoints of the named metrics, e.g. Availability=points(0, 0)."""
    case = copy.deepcopy(CASES[case_id])
    for entry in case["catalog"]:
        name = entry["result"].get("descriptor", {}).get("metric_name")
        if name in series:
            entry["result"]["datapoints"] = series[name]
    by_id = {e["id"]: e for e in case["catalog"]}
    for fact in case["candidate"]["facts"]:  # the model quotes what the tool returned, whatever it returned
        fact["quote"] = diagnosis.scalar(by_id[fact["evidence_id"]], fact["path"])
    catalog = [{**e, "result": safety.bounded(e["result"])} for e in case["catalog"]]
    return diagnosis.validate(safety.text(json.dumps(case["candidate"])), catalog)["status"]


# The silent gap in correlated-hang is 10:00 to 10:10; 10:10 to 10:15 is after it.
def test_hang_survives_healthy_minutes_after_the_gap():
    assert verdict("correlated-hang") == "VALID"
    down = {"Availability": points(0, 0, 0, 0, 0, first=2) + points(1, 1, first=11)}
    fresh = {"TelemetryFresh": points(1, 1, 1, 1, 1, first=2) + points(1, 1, first=11)}
    assert verdict("correlated-hang", **down, **fresh) == "VALID"


def test_hang_needs_the_probe_to_fail_for_most_of_the_gap():
    mostly_up = {"Availability": points(1, 1, 0, 1, 1, first=2)}
    assert verdict("correlated-hang", **mostly_up) == "REJECTED"
    one_in_five = {"Availability": points(0, 1, 1, 1, 1, first=2)}
    assert verdict("correlated-hang", **one_in_five) == "REJECTED"


def test_hang_needs_a_fresh_collector_during_the_gap_not_before_it():
    stale_in_gap = {"TelemetryFresh": points(0, 0, 0, 0, 0, first=2) + points(1, 1, first=11)}
    assert verdict("correlated-hang", **stale_in_gap) == "REJECTED"


def test_resource_pressure_needs_a_sustained_run_not_every_point():
    assert verdict("resource-pressure") == "VALID"
    ramp = {"CPUUtilization": points(40, 42, 91, 93, 95)}
    assert verdict("resource-pressure", **ramp) == "VALID"
    one_spike = {"CPUUtilization": points(96, 40, 41, 42, 43, 44)}
    assert verdict("resource-pressure", **one_spike) == "REJECTED"


def test_unsorted_datapoints_are_judged_in_time_order():
    alternating = points(91, 40, 93, 42, 95, first=2)  # high, low, high, low, high in time
    highs_first = [alternating[0], alternating[2], alternating[4], alternating[1], alternating[3]]
    assert verdict("resource-pressure", CPUUtilization=highs_first) == "REJECTED"
    ramp = points(40, 42, 91, 93, 95, first=2)
    jumbled = [ramp[4], ramp[0], ramp[2], ramp[1], ramp[3]]  # no run in the order returned, a run in time
    assert verdict("resource-pressure", CPUUtilization=jumbled) == "VALID"


@pytest.mark.parametrize(
    "case_id,metric,bad",
    [("known-service-failure", "Availability", 1), ("collector-or-transport-failure", "TelemetryFresh", 1)],
)
def test_service_and_collector_failure_tolerate_healthy_edges_only_when_sustained(case_id, metric, bad):
    sustained = {metric: points(bad, bad, 0, 0, 0, bad)}
    assert verdict(case_id, **sustained) == "VALID"
    scattered = {metric: points(0, bad, bad, 0, bad, bad)}
    assert verdict(case_id, **scattered) == "REJECTED"


def test_a_datapoint_outside_the_window_still_voids_the_evidence():
    late = {"CPUUtilization": points(96, 96, 96) + [{"timestamp": "2026-10-01T11:00:00Z", "value": 96}]}
    assert verdict("resource-pressure", **late) == "REJECTED"


def test_sustained_rule():
    assert not diagnosis.sustained([])
    assert diagnosis.sustained([True])
    assert diagnosis.sustained(
        [False, True, True, True, False, False, False, False, False, False]
    )  # run of 3
    assert diagnosis.sustained([True, True, True, True, False])  # 80 percent
    assert not diagnosis.sustained([True, False, True, False, True, False])
