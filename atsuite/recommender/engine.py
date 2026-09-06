from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from atsuite.pipeline import PROVIDERS, provider_cloud_name, resolve_benchmark
from atsuite.recommender.candidates import generate_candidates
from atsuite.recommender.confidence import assess_confidence
from atsuite.recommender.constraints import constraint_violations
from atsuite.recommender.experiment import ExperimentRunner
from atsuite.recommender.explain import explain
from atsuite.recommender.export import export_recommendation
from atsuite.recommender.metrics import comparison_metrics
from atsuite.recommender.model import CandidateMeasurement, CandidateScore, Recommendation, RecommendationConstraints
from atsuite.recommender.normalize import CurrencyNormalizer
from atsuite.recommender.pareto import pareto_frontier
from atsuite.recommender.scoring import DEFAULT_BALANCED_WEIGHTS, OBJECTIVES, missing_required_metrics, ranking_key, score_candidates, validate_weights
from atsuite.recommender.workload import profile_workload
from atsuite.utils import resolve_benchmark_root


class RecommendationEngine:
    def __init__(self, experiment_runner=None, *, output_dir: Path = Path("results/recommendations"), progress=None):
        self.experiment_runner = experiment_runner
        self.output_dir = Path(output_dir).resolve()
        self.progress = progress or (lambda _: None)

    def recommend(self, config_path: Path, *, providers: list[str] | None = None,
                  runs: int = 5, warmup_runs: int = 1, objective: str = "balanced",
                  weights: dict[str, float] | None = None,
                  constraints: RecommendationConstraints | None = None,
                  fx_rates: dict[str, float] | None = None, latency_stat: str = "p95",
                  keep_resources: bool = False) -> Recommendation:
        if isinstance(runs, bool) or not isinstance(runs, int) or runs < 3:
            raise ValueError("runs must be an integer >= 3; single-run statistics are exploratory only")
        if isinstance(warmup_runs, bool) or not isinstance(warmup_runs, int) or warmup_runs < 0:
            raise ValueError("warmup_runs must be a nonnegative integer")
        if objective not in OBJECTIVES or latency_stat not in {"p50", "p95", "median"}:
            raise ValueError("Unknown objective or latency statistic")
        selected_weights = dict(OBJECTIVES[objective] if weights is None else weights)
        validate_weights(selected_weights)
        constraints = constraints or RecommendationConstraints()
        allowed_clouds = {provider_cloud_name(name) for name in PROVIDERS}
        if constraints.allowed_clouds is not None and not set(constraints.allowed_clouds) <= allowed_clouds:
            raise ValueError(f"Allowed clouds must be from {sorted(allowed_clouds)}")
        normalizer = CurrencyNormalizer(fx_rates)
        config_path = Path(config_path).resolve()
        config_bytes = config_path.read_bytes()
        benchmark = resolve_benchmark_root(config_path).name
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + "-" + uuid4().hex[:8]
        directory = self.output_dir / benchmark
        experiment_dir = directory / stamp
        experiment_dir.mkdir(parents=True, exist_ok=True)
        (experiment_dir / "config.json").write_bytes(config_bytes)
        candidates = generate_candidates(config_path, providers)
        required = {"latency", "reliability"}
        if weights is not None:
            required.update(name for name, weight in weights.items() if weight > 0)
        elif objective != "balanced":
            required.add(objective)
        minimum = max(2, math.ceil(runs * .6))
        result = Recommendation(
            benchmark=benchmark, objective=objective, weights=selected_weights,
            constraints=asdict(constraints), pricing={"normalized_currency": "USD", "fx_rates": normalizer.rates},
            experiment={"id": stamp, "runs": runs, "warmup_runs": warmup_runs, "latency_stat": latency_stat,
                        "min_successful_runs": minimum, "required_metrics": sorted(required),
                        "config_path": str(config_path), "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
                        "config_snapshot": str(experiment_dir / "config.json"),
                        "run_order": [candidate.id for candidate in candidates], "keep_resources": keep_resources,
                        "quantile_method": "linear interpolation at (n-1)*p (type 7)", "stddev_method": "sample (n-1); single sample = 0",
                        "cold_start_policy": "warmup followed by natural cold starts; no forced cold starts",
                        "resource_sizing": "fixed", "outlier_policy": "retain_all",
                        "aggregation": {"latency": latency_stat, "cost": "median", "memory": "median", "state_overhead": "median",
                                        "reliability": "failed_measured_runs / measured_runs", "cold_start": "sum(cold_start_total) / sum(invocation_count)"}},
        )
        runner = self.experiment_runner or ExperimentRunner(output_dir=experiment_dir, fx_rates=fx_rates,
                                                          keep_resources=keep_resources, progress=self.progress)
        for index, candidate in enumerate(candidates, 1):
            self.progress(f"[{index}/{len(candidates)}] {candidate.id}")
            if not candidate.supported:
                measurement = CandidateMeasurement(candidate, status="unsupported")
                row = CandidateScore(measurement, comparison_metrics(measurement), exclusion_reasons=[candidate.unsupported_reason])
            else:
                try:
                    if not result.workload:
                        resolved = resolve_benchmark(config_path, candidate.provider)
                        result.workload = profile_workload(resolved)
                        trace_bytes = resolved.trace_path.read_bytes()
                        (experiment_dir / "trace.json").write_bytes(trace_bytes)
                        result.experiment.update(trace_sha256=hashlib.sha256(trace_bytes).hexdigest(), trace_snapshot=str(experiment_dir / "trace.json"))
                    measurement = runner.run_candidate(candidate, runs=runs, warmup_runs=warmup_runs, uid_prefix=f"recommend-{stamp}")
                except (Exception, SystemExit) as exc:
                    measurement = CandidateMeasurement(candidate, status="experiment_failed",
                                                       diagnostics=[{"kind": "experiment_failed", "error": str(exc)}])
                row = CandidateScore(measurement, comparison_metrics(measurement, latency_stat))
                if measurement.successful_runs < minimum:
                    row.exclusion_reasons.append(f"successful_runs {measurement.successful_runs} < minimum {minimum}")
                if measurement.successful_runs + measurement.failed_runs != runs:
                    row.exclusion_reasons.append("Incomplete measured run count")
                row.exclusion_reasons.extend(missing_required_metrics(row, required))
                row.exclusion_reasons.extend(constraint_violations(row, constraints))
            result.candidates.append(row)
        feasible = [row for row in result.candidates if not row.exclusion_reasons]
        result.experiment.update(requested_candidates=len(candidates), executed_candidates=sum(row.measurement.status != "unsupported" for row in result.candidates),
                                 comparable_candidates=len(feasible))
        result.pareto_frontier = pareto_frontier(feasible)
        for row in feasible:
            row.on_pareto_frontier = row.candidate.id in result.pareto_frontier
        # Compute selection scores after Pareto filtering; retain informational
        # scores for dominated candidates against the same feasible reference.
        frontier = score_candidates([row for row in feasible if row.on_pareto_frontier], selected_weights, reference=feasible)
        dominated = score_candidates([row for row in feasible if not row.on_pareto_frontier], selected_weights, reference=feasible)
        result.rankings = sorted(frontier + dominated, key=ranking_key)
        for name, weight in selected_weights.items():
            values = [row.metrics[name].value for row in feasible if row.metrics[name].status == "available"]
            if weight > 0 and values:
                result.normalization_bounds[name] = {"min": min(values), "max": max(values)}
        if len(feasible) < 2 or not frontier:
            result.explanation.append(f"Recommendation unavailable: {len(feasible)} comparable candidates; at least two are required.")
        else:
            result.winner = frontier[0]
            for name in ("latency", "cost", "reliability"):
                available = [row for row in result.rankings if row.metrics[name].status == "available"]
                if available:
                    setattr(result, f"best_{name}", min(available, key=lambda row: (row.metrics[name].value, ranking_key(row))).candidate.id)
            for family in ("faas", "session"):
                best = next((row for row in result.rankings if row.candidate.family == family), None)
                setattr(result, f"best_{family}", best.candidate.id if best else None)
            balanced = score_candidates([replace(row) for row in feasible], DEFAULT_BALANCED_WEIGHTS, reference=feasible)
            result.best_balanced = next(row.candidate.id for row in balanced if row.on_pareto_frontier)
            comparison = [result.winner] + [row for row in result.rankings if row is not result.winner]
            incomplete = len(feasible) != len(candidates) or any(
                row.metrics[name].status != "available" for row in feasible for name, weight in selected_weights.items() if weight
            )
            result.confidence, result.score_margin = assess_confidence(comparison, selected_weights, incomplete_comparison=incomplete)
        explain(result)
        export_recommendation(result, directory / f"{stamp}.recommendation.json")
        return result
