"""Small controlled sampling plan and comparable-cost crossing brackets."""

import json
from itertools import combinations

from atsuite.provider_model.observation import BACKENDS


def profiling_plan() -> dict:
    """Return an unexecuted, paired one-factor-at-a-time experiment design."""
    baseline = {
        "state_bytes": 1000000,
        "llm_idle_s": 10,
        "load_rps": 0.1,
        "instances": 2,
    }
    levels = {
        "state_bytes": [0, 10000000],
        "llm_idle_s": [1, 60],
        "load_rps": [0.01, 1],
        "instances": [1, 4],
    }
    cases = [{"id": "baseline", "condition": baseline}]
    for feature, values in levels.items():
        for value in values:
            cases.append(
                {"id": f"{feature}-{value}", "condition": {**baseline, feature: value}}
            )
    return {
        "status": "plan_only_no_cloud_actions",
        "providers": [p for p in BACKENDS if p != "local"],
        "cases": cases,
        "pairing": "Seal one equivalent Capture per state/idle variant; replay it on all four backends",
        "controls": [
            "Pin dataset, graph, LangGraph, Tool implementation and per-provider image digests",
            "Measure logical state accesses; configured state size is not measured S_G",
            "Keep Tool compute and workflow fixed while varying one factor",
            "Use actual deployment readback; reject unenforceable instance settings",
            "Separate session slots, RPC concurrency and physical instances",
            "Record window lifetimes and inclusive retention/idle/state bills once",
            "Use cold and warm repetitions, with the same seeded arrivals across providers",
        ],
        "execution": {
            "runner": "experiments.exp1.runner.static_deployment_runner.execute",
            "budget": "Existing reserve_budget and calibrated Pilot/admission are mandatory",
            "unsupported_capacity": "Skip/report unsupported; never treat reserved concurrency as instance count",
        },
        "required_artifacts": [
            "sealed workload manifest",
            "deployment readback",
            "request_metrics.jsonl",
            "provider evidence with collection timestamps",
            "normalized observations with measured slot occupancy",
            "exclusive attributable billing when claiming actual cost",
        ],
    }


def crossover_intervals(predictions: list[dict]) -> list[dict]:
    """Bracket cost ordering changes along an ordered sweep, using complete pool costs.

    predictions contains {x, prediction: ProviderModel.predict(...)}. No FX is
    inferred, no partial cost scopes are compared, and no quantiles are averaged.
    """
    previous, crossings = {}, []
    for point in sorted(predictions, key=lambda p: p["x"]):
        costs = {}
        for pool in point["prediction"]["pools"]:
            for cost in pool["cost"]:
                if (
                    cost["kind"] == "window"
                    and cost["complete"]
                    and cost["total"]["p50"] is not None
                ):
                    # Extrapolated sweeps are not evidence for a crossing.
                    if not cost["coverage"] or any(
                        c["extrapolated_features"] for c in cost["coverage"]
                    ):
                        continue
                    key = (
                        json.dumps(pool["pool"], sort_keys=True),
                        pool["pool"]["provider"],
                        cost["currency"],
                        cost["scope"],
                        cost["training_basis"],
                    )
                    costs[key] = cost["total"]["p50"]
        current = {}
        for left, right in combinations(sorted(costs), 2):
            if left[1] == right[1] or left[2:] != right[2:]:
                continue
            key = (left, right)
            difference = costs[left] - costs[right]
            if key in previous:
                x, old = previous[key]
                if old * difference < 0 or (difference == 0 and old != 0):
                    crossings.append(
                        {
                            "providers": [left[1], right[1]],
                            "pools": [json.loads(left[0]), json.loads(right[0])],
                            "currency": left[2],
                            "scope": left[3],
                            "training_basis": left[4],
                            "interval": [x, point["x"]],
                            "basis": "predicted_median_crossing_bracket",
                        }
                    )
            current[key] = (point["x"], difference)
        previous = current
    return crossings
