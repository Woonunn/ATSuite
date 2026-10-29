"""Shadow policies and matched independent-pool decision diagnostics."""

import math
from collections import Counter


def baseline_choices(
    candidates: list[dict], index: int, rho: float | None, *, rho_threshold: float
) -> dict:
    """Comparison policies only; these do not reserve capacity or claim safety."""
    keys = [c["key"] for c in candidates]
    choices = {f"fixed:{key}": key for key in keys}
    choices["round_robin"] = keys[index % len(keys)] if keys else None
    measured = [c for c in candidates if c["prediction"]["latency"]["mean"] is not None]
    choices["lowest_predicted_mean"] = (
        min(measured, key=lambda c: (c["prediction"]["latency"]["mean"], c["key"]))[
            "key"
        ]
        if measured
        else None
    )
    mode = (
        "state-coupled"
        if rho is not None and rho >= rho_threshold
        else "state-decoupled"
    )
    matching = [c["key"] for c in candidates if c["prediction"]["mode"] == mode]
    choices["rho_rule"] = matching[0] if matching and rho is not None else None
    return choices


def decision_quality(
    decisions: list[dict], paired_outcomes: dict, *, rho_threshold: float
) -> dict:
    """Compare rankings against matched independent runs, never a mixed-run latency.

    paired_outcomes maps request_id -> pool key -> observation. It is scoring-only;
    callers must match task, logical arrival, resource setup and reference version.
    """
    counts, regrets, matches = {}, {}, {}
    for index, decision in enumerate(decisions):
        rho = decision["workload"]["metrics"].get("rho_kb_s", {}).get("mean")
        choices = baseline_choices(
            decision["candidates"],
            decision.get("sequence_index", index),
            rho,
            rho_threshold=rho_threshold,
        )
        choices["risk_aware"] = decision["selected"]
        outcomes = paired_outcomes.get(decision["request_id"], {})
        # Incomplete provider matrices cannot establish an oracle winner.
        complete = set(outcomes) == {c["key"] for c in decision["candidates"]}
        feasible = {
            k: r["measurements"]["latency_s"]
            for k, r in outcomes.items()
            if r["status"] == "success"
            and r["measurements"].get("latency_s") is not None
        }
        best = min(feasible.values()) if feasible and complete else None
        for policy, selected in choices.items():
            counts.setdefault(policy, Counter())[selected or "abstain"] += 1
            if best is not None and selected in feasible:
                regrets.setdefault(policy, []).append(feasible[selected] - best)
            if best is not None and selected in outcomes:
                matches.setdefault(policy, []).append(feasible.get(selected) == best)
    return {
        "selection_counts": {k: dict(v) for k, v in counts.items()},
        "paired_latency_regret_s": {k: sum(v) / len(v) for k, v in regrets.items()},
        "paired_best_fraction": {k: sum(v) / len(v) for k, v in matches.items()},
        "paired_scored": {k: len(v) for k, v in matches.items()},
        "latency_regret_scored": {k: len(v) for k, v in regrets.items()},
        "scope": "Independent-pool ranking diagnostic; not mixed-routing p95, cost, or SLO benefit",
    }


def main() -> None:
    """Replay every 31st historical arrival without altering its actual target."""
    import argparse
    import json
    from pathlib import Path

    from atsuite.provider_model import ProviderModel
    from atsuite.provider_model.e1b import load_e1b
    from atsuite.routing import OnlineRouter
    from atsuite.workload import WorkloadModel

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--e1b", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rho-threshold", type=float, required=True)
    args = parser.parse_args()
    if args.rho_threshold < 0 or not math.isfinite(args.rho_threshold):
        raise ValueError("Invalid rho threshold")
    rows, provenance = load_e1b(args.e1b)
    model, workload = ProviderModel(), WorkloadModel()
    pools, paired = {}, {}
    for row in sorted(rows, key=lambda r: r["available_ns"]):
        provider = row["pool"]["provider"]
        if provider in pools and pools[provider] != row["pool"]:
            raise ValueError("Shadow comparison requires one frozen pool per provider")
        pools[provider] = row["pool"]
        model.update(row)
        workload.update(row["profile"], row["available_ns"])
        paired.setdefault((row["logical_id"], row["task_id"]), {})[provider] = row
    args.output.mkdir(parents=True, exist_ok=False)
    router = OnlineRouter(
        workload, model, pools, {}, log_path=args.output / "decisions.jsonl"
    )
    decisions, outcomes = [], {}
    for index, row in enumerate(sorted(rows, key=lambda r: r["arrival_ns"])):
        if index % 31:
            continue
        rid = "shadow:" + row["id"]
        decision = router.decide(
            rid,
            row["task_id"],
            row["load_rps"],
            as_of_ns=row["arrival_ns"],
            shadow=True,
        )
        decision["sequence_index"] = index
        decisions.append(decision)
        outcomes[rid] = paired[(row["logical_id"], row["task_id"])]
    report = decision_quality(decisions, outcomes, rho_threshold=args.rho_threshold)
    report.update(
        provenance=provenance,
        sampled_decisions=len(decisions),
        stride=31,
        rho_threshold=args.rho_threshold,
        capacity="unknown; no reservations",
        availability="All predictions strictly filtered at historical arrival; paired outcomes are scoring-only",
    )
    (args.output / "diagnostics.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n"
    )
    print(args.output)


if __name__ == "__main__":
    main()
