from atsuite.recommender.model import CandidateScore, METRICS


def pareto_frontier(candidates: list[CandidateScore], dimensions=METRICS) -> list[str]:
    """Only assert dominance between identical sets of fully observed dimensions.

    Pairwise intersections of differing masks can form dominance cycles and can
    eliminate a measured tradeoff just because its competitor lacks evidence.
    """
    values = {
        candidate.candidate.id: {
            name: candidate.metrics[name].value for name in dimensions
            if name in candidate.metrics and candidate.metrics[name].status == "available"
        }
        for candidate in candidates
    }
    return sorted(
        key for key, row in values.items()
        if not any(
            row.keys() == other.keys() and row
            and all(other[name] <= row[name] for name in row)
            and any(other[name] < row[name] for name in row)
            for other_key, other in values.items() if other_key != key
        )
    )
