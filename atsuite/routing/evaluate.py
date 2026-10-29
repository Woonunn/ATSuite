"""Offline causal validation: python -m atsuite.routing.evaluate --help."""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from atsuite.provider_model import ProviderModel
from atsuite.provider_model.e1a import load_e1a
from atsuite.provider_model.e1b import load_e1b
from atsuite.provider_model.model import _compatible, _distribution
from atsuite.routing.router import risk_estimate
from atsuite.workload.profile import number


def evaluate(rows: list[dict], *, slo_s=120.0, held_task=None) -> dict:
    """Score before updating; task holdout never trains on the held task."""
    if not number(slo_s) or slo_s == 0:
        raise ValueError("SLO must be positive and finite")
    model, frozen = ProviderModel(), ProviderModel()
    first = {}
    for row in sorted(rows, key=lambda r: r["available_ns"]):
        if row["kind"] == "request":
            first.setdefault((row["pool"]["provider"], row["id"]), row)
    cutoffs = {}
    for provider in {r["pool"]["provider"] for r in first.values()}:
        arrivals = sorted(
            r["arrival_ns"] for r in first.values() if r["pool"]["provider"] == provider
        )
        cutoffs[provider] = arrivals[int(len(arrivals) * 0.6)]
    events = [(r["arrival_ns"], 0, i, r) for i, r in enumerate(first.values())]
    events += [(r["available_ns"], 1, i, r) for i, r in enumerate(rows)]
    past, predictions, pending = {}, [], {}
    for timestamp, action, _, row in sorted(events, key=lambda e: e[:3]):
        provider = row["pool"]["provider"]
        task = row.get("task_id", "e1a-fixed")
        key = (provider, row["id"])
        if action == 0:
            context = [r for r in past.values() if _compatible(r["pool"], row["pool"])]
            profiles = [r["profile"] for r in context if r["status"] == "success"][-20:]
            if profiles and all(p == profiles[0] for p in profiles):
                profiles = profiles[:1]
            pred = model.predict(
                profiles, [row["pool"]], row["load_rps"], as_of_ns=timestamp
            )["pools"][0]
            fixed = frozen.predict(
                profiles, [row["pool"]], row["load_rps"], as_of_ns=timestamp
            )["pools"][0]
            risk_rows = [
                r
                for r in context
                if r.get("task_id", "e1a-fixed") == task
                and row["load_rps"]
                <= r["load_rps"]
                <= max(row["load_rps"] * 1.25, row["load_rps"] + 0.01)
            ]
            risk = risk_estimate(risk_rows, slo_s)
            result = {
                "id": row["id"],
                "logical_id": row.get("logical_id"),
                "provider": provider,
                "task_id": task,
                "arrival_ns": timestamp,
                "load_rps": row["load_rps"],
                "held_out": task == held_task
                if held_task
                else timestamp >= cutoffs[provider],
                "p50": pred["latency"]["p50"],
                "p95": pred["latency"]["p95"],
                "frozen_p50": fixed["latency"]["p50"],
                "risk": risk,
                "low_confidence": pred["low_confidence"],
                "capacity_unknown": bool(pred["capacity"]["missing"]),
            }
            predictions.append(result)
            pending[key] = result
        else:
            if key in pending:
                pending[key].update(
                    status=row["status"],
                    actual_s=row["measurements"].get("latency_s"),
                    measurements=row["measurements"],
                    source_status=row.get("source_status", row["status"]),
                )
            if task != held_task:
                model.update(row)
                if timestamp < cutoffs[provider]:
                    frozen.update(row)
                if row["kind"] == "request":
                    past[key] = row
    groups = defaultdict(list)
    for r in predictions:
        if r["held_out"]:
            groups[(r["provider"], r["task_id"], r["load_rps"])].append(r)
    summaries = []
    for (provider, task, load), group in groups.items():
        scored = [
            r
            for r in group
            if r["status"] == "success"
            and r["actual_s"] is not None
            and r["p50"] is not None
        ]
        calibrated = [r for r in group if r["risk"]["probability"] is not None]

        def violation(r):
            return (
                r["status"] != "success"
                or r["actual_s"] is None
                or r["actual_s"] > slo_s
            )

        bins = []
        for low, high in [(0, 0.1), (0.1, 0.3), (0.3, 0.6), (0.6, 1.01)]:
            members = [r for r in calibrated if low <= r["risk"]["probability"] < high]
            if members:
                bins.append(
                    {
                        "range": [low, high],
                        "n": len(members),
                        "predicted": sum(r["risk"]["probability"] for r in members)
                        / len(members),
                        "observed": sum(violation(r) for r in members) / len(members),
                    }
                )
        summaries.append(
            {
                "provider": provider,
                "task_id": task,
                "load_rps": load,
                "n": len(group),
                "statuses": dict(Counter(r["source_status"] for r in group)),
                "slo_violation_rate": sum(violation(r) for r in group) / len(group),
                "success_rate": sum(r["status"] == "success" for r in group)
                / len(group),
                "observed_success_p95": _distribution(
                    [
                        r["actual_s"]
                        for r in group
                        if r["status"] == "success" and r["actual_s"] is not None
                    ],
                    len(group),
                )["p95"],
                "mae_s": sum(abs(r["p50"] - r["actual_s"]) for r in scored)
                / len(scored)
                if scored
                else None,
                "p95_attainment": sum(r["actual_s"] <= r["p95"] for r in scored)
                / len(scored)
                if scored
                else None,
                "frozen_mae_s": _distribution(
                    [
                        abs(r["frozen_p50"] - r["actual_s"])
                        for r in scored
                        if r["frozen_p50"] is not None
                    ],
                    len(scored),
                )["mean"],
                "risk_coverage": len(calibrated) / len(group),
                "calibration_bins": bins,
                "brier": sum(
                    (r["risk"]["probability"] - violation(r)) ** 2 for r in calibrated
                )
                / len(calibrated)
                if calibrated
                else None,
                "measurement_coverage": {
                    k: sum(r["measurements"].get(k) is not None for r in group)
                    for k in ["queue_s", "cold_start_s", "harness_queue_s"]
                },
                "cost": None,
            }
        )
    return {
        "split": "leave_task_out" if held_task else "temporal",
        "held_task": held_task,
        "slo_s": slo_s,
        "groups": summaries,
        "predictions": predictions,
        "limitations": [
            "Historical independent pools are not mixed routing outcomes",
            "Unavailable risks are not zero; missing costs are not savings",
            "Wilson intervals assume exchangeability; no production SLO guarantee",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--e1a", type=Path)
    source.add_argument("--e1b", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--slo-s", type=float, default=120)
    args = parser.parse_args()
    rows, provenance = load_e1a(args.e1a) if args.e1a else load_e1b(args.e1b)
    args.output.mkdir(parents=True, exist_ok=False)
    for task in [None] + (sorted({r["task_id"] for r in rows}) if args.e1b else []):
        report = evaluate(rows, slo_s=args.slo_s, held_task=task)
        (args.output / f"{task or 'temporal'}.json").write_text(
            json.dumps(report, allow_nan=False) + "\n"
        )
    (args.output / "provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n"
    )
    print(args.output)


if __name__ == "__main__":
    main()
