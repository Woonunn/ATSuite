import math
from decimal import Decimal

from atsuite.recommender.model import CandidateScore, METRICS
from atsuite.recommender.normalize import min_max_normalize


DEFAULT_BALANCED_WEIGHTS = {
    "latency": .35, "cost": .25, "reliability": .20,
    "cold_start": .08, "state_overhead": .07, "memory": .05,
}
OBJECTIVES = {
    "balanced": DEFAULT_BALANCED_WEIGHTS,
    "latency": {"latency": .65, "reliability": .15, "cold_start": .10, "state_overhead": .05, "cost": .05},
    "cost": {"cost": .65, "latency": .20, "reliability": .10, "memory": .05},
    "reliability": {"reliability": .55, "latency": .25, "cold_start": .10, "cost": .10},
}


def validate_weights(weights: dict[str, float]) -> None:
    if not weights or not set(weights) <= set(METRICS):
        raise ValueError(f"Weights must use these metrics: {', '.join(METRICS)}")
    if any(isinstance(value, bool) or not isinstance(value, (float, int))
           or not math.isfinite(value) or value < 0 for value in weights.values()):
        raise ValueError("Weights must be finite and nonnegative")
    if sum(Decimal(str(value)) for value in weights.values()) != Decimal("1"):
        raise ValueError("Weights must sum to 1.0")


def missing_required_metrics(candidate: CandidateScore, required_metrics: set[str]) -> list[str]:
    return [f"required metric {name}: {candidate.metrics[name].status}"
            for name in sorted(required_metrics) if candidate.metrics[name].status != "available"]


def ranking_key(candidate: CandidateScore):
    def number(value):
        return value if value is not None else math.inf

    return (-candidate.score, number(candidate.measurement.failure_rate),
            number(candidate.measurement.latency_ms.p95),
            number(candidate.metrics["cost"].value) if candidate.metrics["cost"].status == "available" else math.inf,
            candidate.candidate.id)


def score_candidates(candidates: list[CandidateScore], weights: dict[str, float], *,
                     required_metrics: set[str] | None = None,
                     reference: list[CandidateScore] | None = None) -> list[CandidateScore]:
    validate_weights(weights)
    valid = []
    for candidate in candidates:
        candidate.exclusion_reasons.extend(missing_required_metrics(candidate, required_metrics or set()))
        if not candidate.exclusion_reasons:
            valid.append(candidate)
    reference = valid if reference is None else reference
    normalized = {
        name: min_max_normalize({row.candidate.id: row.metrics[name].value
                                if row.metrics[name].status == "available" else None for row in reference})
        for name, weight in weights.items() if weight > 0
    }
    for candidate in valid:
        candidate.normalized_scores = {name: values[candidate.candidate.id]
                                       for name, values in normalized.items() if candidate.candidate.id in values}
        denominator = math.fsum(weights[name] for name in candidate.normalized_scores)
        if not denominator:
            candidate.exclusion_reasons.append("No weighted metrics available")
            continue
        candidate.effective_weights = {name: weights[name] / denominator for name in candidate.normalized_scores}
        candidate.score = math.fsum(value * candidate.effective_weights[name] for name, value in candidate.normalized_scores.items())
    return sorted((candidate for candidate in valid if not candidate.exclusion_reasons), key=ranking_key)
