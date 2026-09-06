import unittest
from pathlib import Path

from atsuite.recommender.model import (
    CandidateMeasurement, DeploymentCandidate, MetricValue, RecommendationConstraints,
)


class ModelTests(unittest.TestCase):
    def test_validation(self):
        candidate = DeploymentCandidate("aws_lambda", "aws_lambda", "aws", "faas", Path("config.json"))
        self.assertIsNone(CandidateMeasurement(candidate).latency_ms.p95)
        for kwargs in ({"value": 0}, {"value": float("nan"), "status": "available"}, {"status": "missing"}):
            with self.assertRaises(ValueError):
                MetricValue(**kwargs)
        for kwargs in ({"max_cost_usd": -1}, {"max_failure_rate": 1.1}, {"allowed_families": ["sandbox"]}):
            with self.assertRaises(ValueError):
                RecommendationConstraints(**kwargs)
        with self.assertRaises(ValueError):
            CandidateMeasurement(candidate, failed_runs=-1)
        with self.assertRaises(ValueError):
            DeploymentCandidate("x", "x", "x", "faas", Path("x"), supported=False)
