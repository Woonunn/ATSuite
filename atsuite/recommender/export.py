import json
from dataclasses import asdict
from pathlib import Path

from atsuite.recommender.model import Recommendation


def recommendation_dict(recommendation: Recommendation) -> dict:
    def candidate_dict(row):
        measurement = asdict(row.measurement)
        candidate = measurement.pop("candidate")
        stats = {name: measurement.pop(name) for name in (
            "latency_ms", "cost_usd", "memory_mb", "cold_start_count", "state_sync_ms",
            "platform_ms", "network_ms", "cold_start_rate", "failure_rate", "invocation_failure_rate",
        )}
        return {
            **candidate, **measurement, "metrics": stats,
            "comparison_metrics": {name: asdict(value) for name, value in row.metrics.items()},
            "normalized_scores": row.normalized_scores, "effective_weights": row.effective_weights,
            "score": row.score, "exclusion_reasons": row.exclusion_reasons,
            "on_pareto_frontier": row.on_pareto_frontier,
        }

    winner = recommendation.winner
    return {
        "schema_version": 1, "benchmark": recommendation.benchmark,
        "experiment": recommendation.experiment,
        "objective": {"name": recommendation.objective, "weights": recommendation.weights},
        "constraints": recommendation.constraints, "pricing": recommendation.pricing,
        "recommendation": {
            "provider": winner.candidate.provider, "cloud": winner.candidate.cloud,
            "family": winner.candidate.family, "score": winner.score,
            "confidence": recommendation.confidence, "score_margin": recommendation.score_margin,
        } if winner else None,
        "confidence": recommendation.confidence,
        "specialized_winners": {name: getattr(recommendation, f"best_{name}")
                                for name in ("latency", "cost", "reliability", "balanced", "faas", "session")},
        "pareto_frontier": recommendation.pareto_frontier,
        "rankings": [row.candidate.id for row in recommendation.rankings],
        "normalization_bounds": recommendation.normalization_bounds,
        "workload": recommendation.workload,
        "explanation": recommendation.explanation, "comparisons": recommendation.comparisons,
        "candidates": [candidate_dict(row) for row in recommendation.candidates],
    }


def export_recommendation(recommendation: Recommendation, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(recommendation_dict(recommendation), indent=2, ensure_ascii=False,
                               allow_nan=False, default=str) + "\n", encoding="utf-8")
    recommendation.report_path = str(path)
    return path
