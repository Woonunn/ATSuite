from atsuite.recommender.model import CandidateScore


def assess_confidence(rankings: list[CandidateScore], weights: dict[str, float], *,
                      incomplete_comparison: bool = False) -> tuple[str, float | None]:
    if len(rankings) < 2:
        return "LOW", None
    winner = rankings[0]
    margin = winner.score - rankings[1].score
    measurement = winner.measurement
    cv = measurement.latency_ms.cv
    complete = {name for name, metric in winner.metrics.items() if metric.status == "available"}
    weighted = {name for name, weight in weights.items() if weight > 0}
    major = {"latency", "cost", "reliability"} & weighted
    critical = any(d.get("kind") in {
        "collector_error", "run_failed", "warmup_failed", "analysis_failed", "cleanup_failed",
    } for d in measurement.diagnostics)
    # A small score gap is weak evidence even with low within-candidate variance.
    if (measurement.successful_runs < 3 or cv is None or cv >= .30 or margin < .08
            or critical or not major <= complete or measurement.exploratory):
        return "LOW", margin
    if measurement.successful_runs >= 5 and cv < .15 and weighted <= complete and not incomplete_comparison:
        return "HIGH", margin
    return "MEDIUM", margin
