"""Offline temporal validation: python -m atsuite.provider_model.experiment --help."""

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from atsuite.provider_model import BACKENDS, ProviderModel, crossover_intervals
from atsuite.provider_model.e1a import load_e1a
from atsuite.provider_model.model import _distribution
from atsuite.tracing.storage import read_text


def evaluate(
    observations: list[dict],
    output: Path,
    *,
    known_profile: dict | None = None,
    training_fraction: float = 0.6,
    neighbors: int = 20,
) -> dict:
    """Predict before arrivals, then ingest evidence; compare updating/frozen models.

    known_profile is allowed only for a workload fixed before the experiment.
    Otherwise use previously completed profiles; never current request outcomes.
    """
    if not 0 < training_fraction < 1:
        raise ValueError("training_fraction must be between zero and one")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    model, frozen = ProviderModel(neighbors), ProviderModel(neighbors)
    first = {}
    for row in observations:
        key = (
            row["pool"]["provider"],
            row["pool"]["environment"],
            row["kind"],
            row["id"],
        )
        if key not in first or row["available_ns"] < first[key]["available_ns"]:
            first[key] = row
    arrivals = sorted(r["arrival_ns"] for r in first.values() if r["kind"] == "request")
    if not arrivals:
        raise ValueError("No completed request observations")
    cutoffs = {}
    for provider in BACKENDS:
        times = sorted(
            r["arrival_ns"]
            for r in first.values()
            if r["kind"] == "request" and r["pool"]["provider"] == provider
        )
        if times:
            cutoffs[provider] = times[
                min(len(times) - 1, int(len(times) * training_fraction))
            ]
    events = [(r["arrival_ns"], 0, i, r) for i, r in enumerate(first.values())]
    events += [(r["available_ns"], 1, i, r) for i, r in enumerate(observations)]
    predictions, previous_profiles = {}, {}
    for timestamp, action, _, row in sorted(events, key=lambda e: e[:3]):
        key = (
            row["pool"]["provider"],
            row["pool"]["environment"],
            row["kind"],
            row["id"],
        )
        cutoff = cutoffs[row["pool"]["provider"]]
        if action == 0:
            workload = (
                known_profile
                if known_profile is not None
                else [
                    p
                    for k, p in previous_profiles.items()
                    if k[1] == row["pool"]["environment"]
                ][-20:]
            )
            args = (workload, [row["pool"]], row["load_rps"])
            kwargs = {
                "as_of_ns": timestamp,
                "horizon_s": (row["completed_ns"] - timestamp) / 1e9
                if row["kind"] == "window"
                else 60,
            }
            online = model.predict(*args, **kwargs)["pools"][0]
            baseline = frozen.predict(*args, **kwargs)["pools"][0]
            predictions[key] = {
                "id": row["id"],
                "provider": row["pool"]["provider"],
                "kind": row["kind"],
                "arrival_ns": timestamp,
                "held_out": timestamp >= cutoff,
                "sample_count": online["sample_count"],
                "predicted_p50": online["latency"]["p50"],
                "predicted_p95": online["latency"]["p95"],
                "frozen_p50": baseline["latency"]["p50"],
                "low_confidence": online["low_confidence"],
                "cost_prediction": online["cost"],
                "load_rps": row["load_rps"],
                "rho_kb_s": row["profile"]["metrics"].get("rho_kb_s"),
            }
        else:
            model.update(row)
            if timestamp < cutoff:
                frozen.update(row)
            if row["kind"] == "request" and row["status"] == "success":
                previous_profiles[key] = row["profile"]
            prediction = predictions.get(key)
            if prediction is not None:
                prediction["actual_latency_s"] = (
                    row["measurements"].get("latency_s")
                    if row["status"] == "success"
                    else None
                )
                prediction["status"] = row["status"]
                prediction["cost_observation"] = row.get("cost")
    rows = list(predictions.values())
    summaries = {}
    for provider in BACKENDS:
        future = [
            r
            for r in rows
            if r["provider"] == provider and r["held_out"] and r["kind"] == "request"
        ]
        scored = [
            r
            for r in future
            if r.get("actual_latency_s") is not None and r["predicted_p50"] is not None
        ]
        paired = [r for r in scored if r["frozen_p50"] is not None]
        costs = [r for r in future if r.get("cost_observation")]
        summaries[provider] = {
            "held_out_requests": len(future),
            "latency_scored": len(scored),
            "prediction_coverage": len(scored) / len(future) if future else 0,
            "latency_mae_s": _distribution(
                [abs(r["predicted_p50"] - r["actual_latency_s"]) for r in scored],
                len(future),
            )["mean"],
            "p95_attainment": sum(
                r["actual_latency_s"] <= r["predicted_p95"] for r in scored
            )
            / len(scored)
            if scored
            else None,
            "paired_online_mae_s": _distribution(
                [abs(r["predicted_p50"] - r["actual_latency_s"]) for r in paired],
                len(paired),
            )["mean"],
            "paired_frozen_mae_s": _distribution(
                [abs(r["frozen_p50"] - r["actual_latency_s"]) for r in paired],
                len(paired),
            )["mean"],
            "actual_cost_observations": sum(
                r["cost_observation"]["basis"] == "actual" for r in costs
            ),
            "estimated_cost_observations": sum(
                r["cost_observation"]["basis"] == "estimated" for r in costs
            ),
            "cost_errors": [],
        }
        groups = defaultdict(list)
        for row in costs:
            observed = row["cost_observation"]
            for pred in row["cost_prediction"]:
                if (
                    pred["kind"] == "request"
                    and pred["scope"] == observed["scope"]
                    and pred["training_basis"] == observed["basis"]
                    and pred["complete"] == observed["complete"]
                ) and pred["subtotal"]["p50"] is not None:
                    groups[(observed["scope"], observed["basis"])].append(
                        abs(
                            pred["subtotal"]["p50"]
                            - sum(observed["components"].values())
                        )
                    )
        for (scope, basis), errors in groups.items():
            summaries[provider]["cost_errors"].append(
                {
                    "scope": scope,
                    "comparison_basis": basis,
                    "currency": BACKENDS[provider][2],
                    "n": len(errors),
                    "mae": sum(errors) / len(errors),
                }
            )
    pools = {json.dumps(r["pool"], sort_keys=True): r["pool"] for r in observations}
    workload = (
        known_profile
        if known_profile is not None
        else list(previous_profiles.values())[-20:]
    )
    sweep = [
        {
            "x": load,
            "prediction": model.predict(
                workload,
                list(pools.values()),
                load,
                as_of_ns=max(r["available_ns"] for r in observations) + 1,
            ),
        }
        for load in sorted({r["load_rps"] for r in observations})
    ]
    crossings = crossover_intervals(sweep)
    comparable = any(
        c["kind"] == "window" and c["complete"]
        for point in sweep
        for p in point["prediction"]["pools"]
        for c in p["cost"]
    )
    report = {
        "cutoff_ns_by_provider": cutoffs,
        "training_fraction": training_fraction,
        "schedule": "strict pre-arrival evidence; arrivals precede updates at timestamp ties",
        "workload_input": "predeclared fixed capture"
        if known_profile is not None
        else "past completed profiles only",
        "providers": summaries,
        "requests": rows,
        "cost_crossover": {
            "axis": "load_rps",
            "intervals": crossings,
            "status": "predicted_sweep"
            if comparable
            else "unverifiable: no complete pool-window costs",
        },
    }
    (output / "load-sweep.json").write_text(
        json.dumps(sweep, indent=2, allow_nan=False) + "\n"
    )
    (output / "validation.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n"
    )
    model.save(output / "model.json")
    plot(report, output)
    return report


def plot(report: dict, output: Path) -> None:
    """Export latency, error evolution, scoped cost and rho coverage figures."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(4, 4, figsize=(17, 13))
    for column, (provider, (_, label, currency)) in enumerate(BACKENDS.items()):
        rows = sorted(
            [
                r
                for r in report["requests"]
                if r["provider"] == provider and r["kind"] == "request"
            ],
            key=lambda r: r["arrival_ns"],
        )
        axes[0, column].set_title(label)
        for key, name in (
            ("actual_latency_s", "Observed"),
            ("predicted_p50", "Predicted p50"),
            ("predicted_p95", "Predicted p95"),
        ):
            points = [
                (i, r.get(key)) for i, r in enumerate(rows) if r.get(key) is not None
            ]
            if points:
                axes[0, column].plot(*zip(*points), label=name, linewidth=0.8)
        axes[0, column].set_ylabel("Latency (s)")
        axes[0, column].set_xlabel("Request index")
        if axes[0, column].lines:
            axes[0, column].legend(fontsize=7)
        for key, label in (("predicted_p50", "Updating"), ("frozen_p50", "Frozen")):
            errors = [
                abs(r[key] - r["actual_latency_s"])
                for r in rows
                if r["held_out"]
                and r.get(key) is not None
                and r.get("actual_latency_s") is not None
            ]
            if errors:
                axes[1, column].plot(
                    [sum(errors[: i + 1]) / (i + 1) for i in range(len(errors))],
                    label=label,
                )
        axes[1, column].set_ylabel("Future cumulative MAE (s)")
        axes[1, column].set_xlabel("Held-out scored request index")
        if axes[1, column].lines:
            axes[1, column].legend(fontsize=7)
        observed_costs, predicted_costs = [], []
        for i, row in enumerate(rows):
            cost = row.get("cost_observation")
            if cost:
                observed_costs.append((i, sum(cost["components"].values())))
                preds = [
                    c
                    for c in row["cost_prediction"]
                    if c["kind"] == "request"
                    and c["scope"] == cost["scope"]
                    and c["training_basis"] == cost["basis"]
                ]
                if preds and preds[0]["subtotal"]["p50"] is not None:
                    predicted_costs.append((i, preds[0]["subtotal"]["p50"]))
        for points, color in (
            (observed_costs, "tab:blue"),
            (predicted_costs, "tab:orange"),
        ):
            if points:
                axes[2, column].scatter(*zip(*points), s=3, color=color)
        axes[2, column].set_ylabel(f"Scoped subtotal ({currency})")
        axes[2, column].set_xlabel("Request index")
        scope = next(
            (r["cost_observation"]["scope"] for r in rows if r.get("cost_observation")),
            "unavailable",
        )
        axes[2, column].set_title(
            scope + "\nestimate blue; prediction orange", fontsize=8
        )
        points = [
            (r["rho_kb_s"], r["actual_latency_s"])
            for r in rows
            if r["rho_kb_s"] is not None and r.get("actual_latency_s") is not None
        ]
        if points:
            axes[3, column].scatter(*zip(*points), s=5)
        else:
            axes[3, column].text(
                0.5,
                0.5,
                "Logical S_G / rho unavailable",
                ha="center",
                transform=axes[3, column].transAxes,
            )
        axes[3, column].set_xlabel("rho (kB/s)")
        axes[3, column].set_ylabel("Observed latency (s)")
    figure.tight_layout()
    figure.savefig(output / "validation.png", dpi=160)
    plt.close(figure)
    figure, ax = plt.subplots(figsize=(8, 3))
    intervals = report["cost_crossover"]["intervals"]
    for i, interval in enumerate(intervals):
        ax.plot(
            interval["interval"],
            [i, i],
            linewidth=4,
            label=" / ".join(interval["providers"]),
        )
    if intervals:
        ax.legend()
    else:
        ax.text(
            0.5,
            0.5,
            report["cost_crossover"]["status"],
            ha="center",
            transform=ax.transAxes,
        )
    ax.set_xlabel("Load (requests/s)")
    ax.set_title("Complete pool cost crossover brackets")
    figure.tight_layout()
    figure.savefig(output / "cost-crossover.png", dpi=160)
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--e1a", type=Path, help="Existing E1-A matrix/run directory; no cloud access"
    )
    source.add_argument(
        "--observations", type=Path, help="JSON list of normalized observations"
    )
    source.add_argument(
        "--plan",
        action="store_true",
        help="Write a controlled experiment plan; never execute it",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--training-fraction", type=float, default=0.6)
    args = parser.parse_args()
    if args.plan:
        from atsuite.provider_model import profiling_plan

        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "profiling-plan.json").write_text(
            json.dumps(profiling_plan(), indent=2) + "\n"
        )
        print(str(args.output / "profiling-plan.json"))
        return
    if args.e1a:
        rows, provenance = load_e1a(args.e1a)
        known = rows[0]["profile"]
    else:
        rows = json.loads(read_text(args.observations))
        provenance = {
            "source": str(args.observations),
            "sha256": hashlib.sha256(args.observations.read_bytes()).hexdigest(),
        }
        known = None
    report = evaluate(
        rows, args.output, known_profile=known, training_fraction=args.training_fraction
    )
    provenance["pricing_source_sha256"] = hashlib.sha256(
        (Path(__file__).parents[1] / "analysis/pricing.py").read_bytes()
    ).hexdigest()
    (args.output / "provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n"
    )
    print(json.dumps(report["providers"], indent=2))


if __name__ == "__main__":
    main()
