"""Versioned deterministic controls, optionally repeated paid synthetic model evaluation."""

import argparse
import copy
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from infra.security_ops import private_write  # noqa: E402
from tracaus import diagnosis, model_api, runtime, safety, work_policy  # noqa: E402


def suite():
    path = ROOT / "evaluations/diagnostics/cases.json"
    data = json.loads(path.read_text())
    evidence = data.pop("evidence")  # shared catalog entries; cases list them by name
    for case in data["cases"]:
        if unknown := [name for name in case["catalog"] if name not in evidence]:
            raise ValueError(f"Case {case['id']!r} references unknown evidence {unknown}")
        case["catalog"] = [copy.deepcopy(evidence[name]) for name in case["catalog"]]
    return data, hashlib.sha256(path.read_bytes()).hexdigest()


def offline():
    data, sha = suite()
    grades = []
    for case in data["cases"]:
        catalog = [{**e, "result": safety.bounded(e["result"])} for e in case["catalog"]]
        candidate = safety.text(json.dumps(case["candidate"]))
        result = diagnosis.validate(candidate, catalog)
        controls = "fixture-sensitive-marker" not in json.dumps(
            catalog
        ) and "person@example.invalid" not in json.dumps(catalog)
        grades.append(
            {
                "case": case["id"],
                "pass": controls and result["status"] == case["expected"],
                "validation": result["status"],
            }
        )
    return {
        "version": 1,
        "suite": data["suite"],
        "fixture_sha256": sha,
        "policy": diagnosis.VERSION,
        "prompt_sha256": hashlib.sha256((ROOT / "agent-instruction.txt").read_bytes()).hexdigest(),
        "source": "reference_answers_and_deterministic_controls",
        "model": None,
        "status": "PASS" if all(g["pass"] for g in grades) else "FAIL",
        "cases": grades,
        "live_model_evaluation": "NOT_RUN",
        "customer_threshold_approval": "PENDING",
    }


class FixtureTools:
    """Read-only injected fixtures; never invoke tools/AWS regardless of model requests."""

    def __init__(self, catalog, reserve=None):
        self.catalog, self.reserve = catalog, reserve

    def invoke(self, name, args, deadline):
        if (
            name not in {"fetch_logs", "fetch_metrics"}
            or args.get("instance_id") != "i-0123456789abcdef0"
            or time.time() >= deadline
        ):
            raise runtime.RuntimeStop("UNAUTHORIZED_FIXTURE_TOOL")
        runtime.validate(
            args,
            runtime.tool_configuration()["tools"][0 if name == "fetch_logs" else 1]["toolSpec"][
                "inputSchema"
            ]["json"],
        )
        if self.reserve:
            self.reserve({"tool_calls": 1, **({"log_queries": 2} if name == "fetch_logs" else {})})
        candidates = [
            e for e in self.catalog if e["tool"] == name and e["instance_id"] == args["instance_id"]
        ]
        if name == "fetch_logs":
            if not args.get("log_group_name"):
                groups = sorted({e["result"]["log_group"] for e in candidates})
                hints = {
                    e["result"]["descriptor"]["metric_id"]: e["result"]["descriptor"]["metric_name"]
                    for e in self.catalog
                    if e["tool"] == "fetch_metrics" and e["instance_id"] == args["instance_id"]
                }
                return {
                    "instance_id": args["instance_id"],
                    "complete": True,
                    "truncated": False,
                    "status": "log_groups_found" if groups else "no_log_groups_found",
                    "log_groups": groups,
                    "metric_catalog_complete": True,
                    "metric_catalog": [{"metric_id": i, "metric_name": n} for i, n in sorted(hints.items())],
                }, True
            candidates = [e for e in candidates if e["result"].get("log_group") == args["log_group_name"]]
        else:
            candidates = [
                e
                for e in candidates
                if (
                    e["result"]["descriptor"]["metric_id"] == args["metric_id"]
                    if args.get("metric_id")
                    else e["result"]["descriptor"]["metric_name"] == args.get("metric_name", "CPUUtilization")
                )
                and (not args.get("namespace") or e["result"]["descriptor"]["namespace"] == args["namespace"])
            ]
        if len(candidates) != 1:
            raise runtime.RuntimeStop("FIXTURE_EVIDENCE_UNAVAILABLE")
        result = safety.bounded(candidates[0]["result"])
        runtime.validate(
            result, runtime.operation(name)["responses"]["200"]["content"]["application/json"]["schema"]
        )
        return result, result.get("complete") is True and not result.get("truncated", False)


class EvaluationBudget:
    def __init__(self, tokens):
        self.tokens, self.used = tokens, dict.fromkeys(runtime.COUNTERS, 0)

    def reserve(self, delta):
        if set(delta) - set(self.used) or any(type(v) is not int or v < 1 for v in delta.values()):
            raise ValueError("Invalid evaluation reservation")
        if self.used["tokens_reserved"] + delta.get("tokens_reserved", 0) > self.tokens:
            raise runtime.RuntimeStop("EVALUATION_BUDGET_EXHAUSTED")
        for key, value in delta.items():
            self.used[key] += value


def live(model, region, repeats, token_budget):
    if not 3 <= repeats <= 5 or not 2048 <= token_budget <= 100000:
        raise ValueError("Select 3–5 repetitions and 2,048–100,000 total reserved tokens")
    data, sha = suite()
    policy = runtime.Limits(
        **{
            **work_policy.DEFAULT["chat_limits"],
            "tokens_reserved": min(work_policy.DEFAULT["chat_limits"]["tokens_reserved"], token_budget),
        }
    )
    total = EvaluationBudget(token_budget)
    client = model_api.from_env() or runtime.sdk_client("bedrock-runtime", region)
    grades = []
    # Valid reference cases are the held-out behavior targets; adversarial invalid
    # candidates are tested deterministically, not supplied as model answers.
    for case in [c for c in data["cases"] if c["expected"] == "VALID"]:
        for repeat in range(repeats):
            per_case = runtime.MemoryBudget(policy)

            def reserve(delta):
                total.reserve(delta)
                return per_case.reserve(delta)

            result = runtime.run(
                case["prompt"],
                model_id=model,
                region=region,
                tools=FixtureTools(case["catalog"], reserve),
                reserve=reserve,
                limits=policy,
                deadline=time.time() + 180,
                client=client,
            )
            quality = result.get("diagnosis", {})
            kinds = {f["kind"] for f in quality.get("report", {}).get("findings", [])}
            grades.append(
                {
                    "case": case["id"],
                    "repeat": repeat,
                    "pass": quality.get("status") == "VALID" and case["expected_kind"] in kinds,
                    "code": result.get("code"),
                    "usage": result.get("usage"),
                    "diagnosis": quality if quality.get("status") == "VALID" else {"status": "UNAVAILABLE"},
                    "sources": result.get("sources", []),
                }
            )
    rate = sum(g["pass"] for g in grades) / len(grades)
    return {
        "version": 1,
        "suite": data["suite"],
        "fixture_sha256": sha,
        "prompt_sha256": hashlib.sha256((ROOT / "agent-instruction.txt").read_bytes()).hexdigest(),
        "policy": diagnosis.VERSION,
        "model": model,
        "region": region,
        "repeats": repeats,
        "source": "paid_synthetic_model_runs",
        "status": "PASS_PROVISIONAL" if rate >= data["thresholds"]["live_diagnosis_pass_rate"] else "FAIL",
        "pass_rate": rate,
        "reserved": total.used,
        "cases": grades,
        "customer_threshold_approval": "PENDING",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live-model")
    parser.add_argument("--region")
    parser.add_argument("--allow-paid-model", action="store_true")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--token-budget", type=int, default=32000)
    args = parser.parse_args()
    try:
        if args.live_model:
            if not args.allow_paid_model or not args.region:
                raise ValueError("Paid evaluation requires explicit authorization and region")
            import os

            os.environ["TRACAUS_DIAGNOSTIC_POLICY"] = diagnosis.VERSION
            result = live(args.live_model, args.region, args.repeats, args.token_budget)
            private_write(args.output, result)
        else:
            result = offline()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(
            json.dumps(
                {"status": result["status"], "cases": len(result["cases"]), "source": result["source"]}
            )
        )
        return 0 if result["status"].startswith("PASS") else 1
    except Exception:
        print(
            "Evaluation failed or exceeded its allowance; no automatic retry. Keep private evidence.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
