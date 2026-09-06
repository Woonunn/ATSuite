import unittest
from pathlib import Path

from atsuite.recommender.constraints import constraint_violations
from atsuite.recommender.metrics import comparison_metrics, metric_stats
from atsuite.recommender.model import CandidateMeasurement, CandidateScore, DeploymentCandidate, MetricValue, RecommendationConstraints


def score_candidate(provider="aws_lambda", latency=100, cost=0.01, failure=0, memory=100, cold=0.1, state=20):
    measurement = CandidateMeasurement(
        DeploymentCandidate(provider, provider, "aws", "faas" if provider == "aws_lambda" else "session", Path("config")),
        successful_runs=5, failure_rate=failure, status="success",
        latency_ms=metric_stats([latency] * 5), cost_usd=metric_stats([cost] * 5) if cost is not None else metric_stats([]),
        memory_mb=metric_stats([memory] * 5), state_sync_ms=metric_stats([state] * 5),
        cold_start_rate=MetricValue(cold, "available"),
    )
    return CandidateScore(measurement, comparison_metrics(measurement))


class ConstraintTests(unittest.TestCase):
    def test_each_threshold_and_boundary(self):
        candidate = score_candidate(failure=0.1)
        for field, value in (("max_p95_latency_ms", 100), ("max_cost_usd", .01), ("max_memory_mb", 100), ("max_failure_rate", .1)):
            self.assertEqual(constraint_violations(candidate, RecommendationConstraints(**{field: value})), [])
            self.assertEqual(len(constraint_violations(candidate, RecommendationConstraints(**{field: value / 2}))), 1)

    def test_missing_and_multiple_constraints(self):
        candidate = score_candidate(cost=None)
        reasons = constraint_violations(candidate, RecommendationConstraints(
            allowed_clouds=["gcp"], allowed_families=["session"], max_cost_usd=1,
        ))
        self.assertEqual(len(reasons), 3)
        self.assertIn("cannot verify", reasons[-1])
