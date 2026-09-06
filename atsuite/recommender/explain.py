from atsuite.recommender.model import Recommendation


FAIRNESS_NOTE = (
    "This recommendation compares the deployment shapes defined by the benchmark configuration; "
    "it does not claim that the runtime families received identical resource allocations."
)


def explain(recommendation: Recommendation) -> None:
    recommendation.explanation.append(FAIRNESS_NOTE)
    winner = recommendation.winner
    if winner is None:
        return
    rows = {row.candidate.id: row for row in recommendation.rankings}
    other = next((row for row in recommendation.rankings if row is not winner), None)
    comparisons = {
        "runner_up": other.candidate.id if other else None,
        "cheapest": recommendation.best_cost, "fastest": recommendation.best_latency,
        "best_faas": recommendation.best_faas, "best_session": recommendation.best_session,
    }
    for row in recommendation.rankings:
        comparisons.setdefault(f"cloud_{row.candidate.cloud}", row.candidate.id)
    for label, provider in comparisons.items():
        if provider is None or provider == winner.candidate.id:
            continue
        comparison = {"candidate": provider, "metrics": {}}
        for metric, left in winner.metrics.items():
            right = rows[provider].metrics[metric]
            if left.status != "available" or right.status != "available":
                continue
            delta = left.value - right.value
            percent = delta / right.value * 100 if right.value else (0.0 if not delta else None)
            comparison["metrics"][metric] = {"winner": left.value, "other": right.value, "difference": delta, "percent_change": percent}
        recommendation.comparisons[label] = comparison
    if other:
        for metric in ("latency", "cost", "state_overhead"):
            comparison = recommendation.comparisons["runner_up"]["metrics"].get(metric)
            if comparison is None:
                continue
            percent = comparison["percent_change"]
            name = f"{recommendation.experiment['latency_stat'].upper()} latency" if metric == "latency" else metric.replace("_", " ")
            if percent is None:
                recommendation.explanation.append(f"{name}: {comparison['winner']:g} versus {comparison['other']:g} for {other.candidate.id} (zero baseline).")
            elif percent:
                recommendation.explanation.append(f"{abs(percent):.1f}% {'lower' if percent < 0 else 'higher'} {name} than {other.candidate.id}.")
        if recommendation.score_margin is not None and recommendation.score_margin < .08:
            recommendation.explanation.append(f"{winner.candidate.id} ranked first, but {other.candidate.id} is very close in score; these measurements do not establish a definitive winner.")
    if winner.measurement.failed_runs == 0:
        recommendation.explanation.append("No measured workflow failures for the winner.")
    omitted = [name for name, weight in recommendation.weights.items() if weight and name not in winner.effective_weights]
    if omitted:
        recommendation.explanation.append(f"Winner weights were renormalized without unavailable/partial metrics: {', '.join(omitted)}.")
    recommendation.explanation.append(f"Ranked by measured evidence under the {recommendation.objective} objective and recorded effective weights.")
