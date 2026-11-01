"""Causal task-holdout evaluation; historical shadow choices are not mixed-cloud savings."""

import argparse
import copy
import hashlib
import json
from collections import Counter
from pathlib import Path

from atsuite.provider_model import ProviderModel
from atsuite.provider_model.analytical import predict_cost
from atsuite.provider_model.e1a import load_e1a
from atsuite.provider_model.e1b import load_e1b
from atsuite.routing import OnlineRouter
from atsuite.workload import WorkloadModel, describe
from atsuite.workload.model import distribution
from atsuite.workload.profile import number

TOOLS = ["prepare_workspace", "execute_analysis", "merge_artifacts", "validate_result"]


def evaluate(
    rows: list[dict],
    descriptors: dict,
    tariffs: dict,
    *,
    held_task=None,
    history_limit=20,
    adaptive=False,
    latency_weight=0.001,
    fx=None,
    prediction_stride=1,
) -> dict:
    """Separate task identities, freeze every prediction, ingest only after availability.

    Virtual capacities and budgets are explicitly shadow-only. The actual dataset
    contains independent provider runs, not outcomes under this routing policy.
    """
    if not isinstance(prediction_stride, int) or prediction_stride < 1:
        raise ValueError("Prediction stride must be positive")
    provider, workload = ProviderModel(tariffs=tariffs), WorkloadModel()
    pools = {r["pool"]["provider"]: r["pool"] for r in rows if r["kind"] == "request"}
    first, latest = {}, {}
    for row in sorted(rows, key=lambda r: r["available_ns"]):
        if row["kind"] == "request":
            first.setdefault((row["pool"]["provider"], row["id"]), row)
            latest[(row["pool"]["provider"], row["id"])] = row
    fallback = "aws_lambda" if "aws_lambda" in pools else next(iter(pools))
    router = OnlineRouter(
        workload,
        provider,
        pools,
        dict.fromkeys(pools, 1),
        default_pool=fallback,
        latency_weight=latency_weight,
        fx=fx,
        execution_bounds={
            k: {
                "timeout_s": 3600,
                "max_cost": 1,
                "currency": tariffs[k]["currency"],
                "tools": TOOLS,
                "evidence": [
                    "virtual shadow executor only; not cloud capacity evidence"
                ],
            }
            for k in pools
        },
    )
    arrivals = sorted(
        first.values(), key=lambda r: (r["arrival_ns"], r["pool"]["provider"], r["id"])
    )
    events = [(r["arrival_ns"], 0, i, r) for i, r in enumerate(arrivals)]
    events += [
        (r["available_ns"], 1, i, r)
        for i, r in enumerate(rows)
        if r["kind"] == "request"
    ]
    training_ids, counts, predictions = set(), Counter(), []
    seen_workload = set()
    seen_fixed_profiles = set()
    for stamp, action, index, row in sorted(events, key=lambda e: e[:3]):
        task, backend = row.get("task_id", "e1a-fixed"), row["pool"]["provider"]
        identity = (backend, row["id"])
        if action:
            allowed = task != held_task and (
                identity in training_ids or counts[backend] < history_limit
            )
            if allowed or (adaptive and (held_task is None or task == held_task)):
                if identity not in training_ids:
                    training_ids.add(identity)
                    counts[backend] += 1
                provider.update(row)
                if identity not in seen_workload and row["status"] == "success":
                    profile = copy.deepcopy(row["profile"])
                    profile["request_id"] = backend + ":" + row["id"]
                    signature = json.dumps(
                        {
                            k: profile[k]
                            for k in ("metrics", "workflow", "tool_durations")
                        },
                        sort_keys=True,
                    )
                    if held_task is not None or signature not in seen_fixed_profiles:
                        workload.update(
                            profile, stamp, descriptor=descriptors.get(task)
                        )
                        seen_fixed_profiles.add(signature)
                    seen_workload.add(identity)
            continue
        if (held_task is not None and task != held_task) or index % prediction_stride:
            continue
        prediction = workload.predict(stamp, descriptor=descriptors.get(task))
        # Predict the actual historical pool separately; deployments may differ across E1-A windows.
        pred = provider.predict(
            prediction, [row["pool"]], row["load_rps"], as_of_ns=stamp
        )["pools"][0]
        decision = router.decide(
            "shadow:" + row["id"],
            load_rps=row["load_rps"],
            as_of_ns=stamp,
            shadow=True,
            descriptor=descriptors.get(task),
        )
        estimate = pred.get("analytical_cost")
        # The reference is measured held-out demand repriced at a frozen tariff, never an invoice.
        reference = predict_cost([row["profile"]], row["pool"], tariffs[backend], [])
        predicted_cost = (
            estimate["total"]["mean"] if estimate and estimate["complete"] else None
        )
        observed_cost = reference["total"]["mean"] if reference["complete"] else None
        attributable = latest[identity].get("cost")
        comparable_charge = attributable and attributable["scope"] in {
            "lambda_invocations",
            "fc_active_invocations",
        }
        charge = sum(attributable["components"].values()) if comparable_charge else None
        compute = (
            sum(
                estimate["execution_details"][k]["mean"]
                for k in attributable["components"]
            )
            if estimate
            and estimate["execution_complete"]
            and comparable_charge
            and set(attributable["components"]) <= estimate["execution_details"].keys()
            else None
        )
        predictions.append(
            {
                "request_id": row["id"],
                "task_id": task,
                "provider": backend,
                "arrival_ns": stamp,
                "selected": decision["selected"],
                "reason": decision["reason"],
                "training_count": len(training_ids),
                "workload_count": prediction["sample_count"],
                "match_basis": prediction["match_basis"],
                "latency": pred["latency"],
                "observed_latency_s": row["measurements"].get("latency_s"),
                "cost": predicted_cost,
                "demand_priced_reference": observed_cost,
                "currency": tariffs[backend]["currency"],
                "cost_absolute_error": abs(predicted_cost - observed_cost)
                if predicted_cost is not None and observed_cost is not None
                else None,
                "attributable_compute_estimate": charge,
                "compute_scope": attributable["scope"] if comparable_charge else None,
                "compute_components": sorted(attributable["components"])
                if comparable_charge
                else [],
                "predicted_compute": compute,
                "compute_absolute_error": abs(compute - charge)
                if compute is not None and charge is not None
                else None,
                "actual_bill": None,
                "missing_cost": estimate["missing"] if estimate else ["no_tariff"],
                "calibration_samples": estimate["calibration"]["sample_count"]
                if estimate
                else 0,
                "pareto": decision["pareto"],
            }
        )
        router.decisions.clear()

    return {
        "held_task": held_task,
        "history_limit": history_limit,
        "prediction_stride": prediction_stride,
        "adaptive": adaptive,
        "latency_weight": latency_weight,
        "predictions": predictions,
        "selection_rate": sum(p["selected"] is not None for p in predictions)
        / len(predictions)
        if predictions
        else None,
        "routing_distribution": dict(Counter(p["selected"] for p in predictions)),
        "latency_error_s": distribution(
            [
                abs(p["latency"]["mean"] - p["observed_latency_s"])
                for p in predictions
                if number(p["latency"]["mean"]) and number(p["observed_latency_s"])
            ],
            len(predictions),
        ),
        "cost_errors": {
            currency: distribution(
                [
                    p["cost_absolute_error"]
                    for p in predictions
                    if p["currency"] == currency
                    and p["cost_absolute_error"] is not None
                ],
                sum(p["currency"] == currency for p in predictions),
            )
            for currency in {p["currency"] for p in predictions}
        },
        "scope": "Historical shadow with virtual capacity; observed independent-pool latency and repriced demand, not mixed-routing benefit or actual bills",
    }


def plot(reports: list[dict], output: Path) -> None:
    """Export distributions and accounting diagnostics, keeping currencies separate."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    selected = [r for r in reports if r["history_limit"] == 20 and not r["adaptive"]]
    rows = [p for report in selected for p in report["predictions"]]
    fig, axes = plt.subplots(2, 3, figsize=(15, 8))
    for backend in sorted({p["provider"] for p in rows}):
        group = [p for p in rows if p["provider"] == backend]
        latencies = sorted(
            p["observed_latency_s"] for p in group if number(p["observed_latency_s"])
        )
        if latencies:
            axes[0, 0].plot(
                latencies,
                [(i + 1) / len(latencies) for i in range(len(latencies))],
                label=backend,
            )
        known = [
            p
            for p in sorted(group, key=lambda p: p["arrival_ns"])
            if p["demand_priced_reference"] is not None
        ]
        total, cumulative = 0, []
        for p in known:
            total += p["demand_priced_reference"]
            cumulative.append(total)
        if known:
            axes[0, 1 if known[0]["currency"] == "USD" else 2].plot(
                range(1, len(known) + 1),
                cumulative,
                label=f"{backend} ({known[0]['currency']})",
            )
    axes[0, 0].set(xlabel="Observed independent-pool latency (s)", ylabel="CDF")
    axes[0, 1].set(
        xlabel="Measured requests (per backend)",
        ylabel="Cumulative repriced marginal cost (USD)",
    )
    axes[0, 2].set(
        xlabel="Measured requests (per backend)",
        ylabel="Cumulative repriced marginal cost (CNY)",
    )
    for ax in axes[0, 1:]:
        if not ax.lines:
            ax.text(
                0.5, 0.5, "Insufficient measured demand for total cost",
                ha="center", va="center", transform=ax.transAxes, fontsize=9,
            )
            ax.set_xticks([])
            ax.set_yticks([])
    routing = Counter(p["selected"] for p in rows)
    axes[1, 0].bar(list(routing), list(routing.values()))
    axes[1, 0].set_title("Shadow choices with declared virtual capacity")
    for adaptive in (False, True):
        points = []
        for history in sorted({r["history_limit"] for r in reports}):
            values = [
                r["latency_error_s"]["mean"]
                for r in reports
                if r["history_limit"] == history
                and r["adaptive"] == adaptive
                and r["latency_error_s"]["mean"] is not None
            ]
            if values:
                points.append((history, sum(values) / len(values)))
        if points:
            axes[1, 1].plot(
                *zip(*points),
                marker="o",
                label="online feedback" if adaptive else "frozen model",
            )
    axes[1, 1].set(
        xlabel="Initial samples per provider", ylabel="Mean latency absolute error (s)"
    )
    coverage = {
        b: sum(p["cost"] is not None for p in rows if p["provider"] == b)
        / sum(p["provider"] == b for p in rows)
        for b in sorted({p["provider"] for p in rows})
    }
    axes[1, 2].bar(list(coverage), list(coverage.values()))
    axes[1, 2].set(ylabel="Complete analytical input coverage", ylim=(0, 1))
    axes[1, 2].tick_params(axis="x", rotation=20)
    for ax in (axes[0, 0], axes[0, 1], axes[0, 2], axes[1, 1]):
        if ax.get_legend_handles_labels()[0]:
            ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(output / "generalization.png", dpi=150)
    fig.savefig(output / "generalization.pdf")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--e1b", type=Path)
    source.add_argument("--e1a", type=Path)
    parser.add_argument(
        "--tariffs",
        type=Path,
        default=Path("experiments/exp1/configs/stage6-tariffs.json"),
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("benchmarks/DataSciBench/tasks/manifest.json"),
    )
    parser.add_argument(
        "--fx", type=Path, default=Path("experiments/exp1/configs/stage6-fx.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    rows, provenance = load_e1b(args.e1b) if args.e1b else load_e1a(args.e1a)
    tariffs = json.loads(args.tariffs.read_text())
    fx = json.loads(args.fx.read_text())
    descriptors = {
        t["task_id"]: describe(
            prompt=t["prompt"], files=t["readiness"]["inputs"]["files"], tools=TOOLS
        )
        for t in json.loads(args.catalog.read_text())["tasks"]
        if t.get("readiness", {}).get("inputs", {}).get("files") is not None
    }
    held_tasks = (
        sorted({r.get("task_id", "e1a-fixed") for r in rows}) if args.e1b else [None]
    )
    reports = []
    for held in held_tasks:
        for history in (0, 3, 20):
            for adaptive in (False, True):
                report = evaluate(
                    rows,
                    descriptors,
                    tariffs,
                    held_task=held,
                    history_limit=history,
                    adaptive=adaptive,
                    fx=fx,
                    prediction_stride=10 if args.e1a else 1,
                )
                name = f"{held or 'temporal'}-{history}-{'adaptive' if adaptive else 'frozen'}"
                (args.output / (name + ".json")).write_text(
                    json.dumps(report, indent=2, allow_nan=False) + "\n"
                )
                reports.append(report)
        print(held or "temporal", "complete", flush=True)
    plot(reports, args.output)
    summary = {
        "provenance": provenance,
        "inputs": {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (args.tariffs, args.catalog, args.fx)
        },
        "reports": [
            {k: v for k, v in r.items() if k != "predictions"} for r in reports
        ],
        "real_joint_cloud_benefit": None,
        "actual_bill_error": None,
    }
    (args.output / "summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False) + "\n"
    )


if __name__ == "__main__":
    main()
