from atsuite.recommender.model import CandidateScore, RecommendationConstraints


def constraint_violations(candidate: CandidateScore, constraints: RecommendationConstraints) -> list[str]:
    reasons = []
    for name, value in (("allowed_clouds", candidate.candidate.cloud), ("allowed_families", candidate.candidate.family)):
        allowed = getattr(constraints, name)
        if allowed is not None and value not in allowed:
            reasons.append(f"{name}: {value} is not allowed")
    checks = {
        "max_p95_latency_ms": (candidate.measurement.latency_ms.p95, candidate.measurement.latency_ms.status),
        "max_cost_usd": (candidate.metrics["cost"].value, candidate.metrics["cost"].status),
        "max_failure_rate": (candidate.metrics["reliability"].value, candidate.metrics["reliability"].status),
        "max_memory_mb": (candidate.metrics["memory"].value, candidate.metrics["memory"].status),
    }
    for name, (value, status) in checks.items():
        limit = getattr(constraints, name)
        if limit is None:
            continue
        if status != "available" or value is None:
            reasons.append(f"{name}: metric {status}; cannot verify constraint")
        elif value > limit:
            reasons.append(f"{name}: {value} exceeds {limit}")
    return reasons
