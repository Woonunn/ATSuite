import unittest

from atsuite.recommender.confidence import assess_confidence
from atsuite.recommender.model import MetricValue
from atsuite.recommender.scoring import OBJECTIVES, score_candidates, validate_weights
from tests.test_recommender_constraints import score_candidate


class ScoringTests(unittest.TestCase):
    def test_best_and_tradeoff_weights(self):
        for weights in OBJECTIVES.values():
            validate_weights(weights)
            a, b = score_candidate("A", 1, 1, 0, 1, 0, 1), score_candidate("B", 2, 2, .1, 2, .1, 2)
            self.assertEqual(score_candidates([b, a], weights)[0].candidate.id, "A")
        for objective, expected in (("latency", "fast"), ("cost", "cheap"), ("balanced", "fast")):
            rows = score_candidates([score_candidate("fast", 1200, .002), score_candidate("cheap", 2000, .001)], OBJECTIVES[objective])
            self.assertEqual(rows[0].candidate.id, expected)

    def test_invalid_weights(self):
        for weights in ({}, {"latency": .9}, {"foo": 1}, {"latency": float("nan")}, {"latency": 1.1, "cost": -.1}):
            with self.assertRaises(ValueError):
                validate_weights(weights)

    def test_optional_missing_reweight_and_required_invalid(self):
        a = score_candidate("A")
        a.metrics["memory"] = MetricValue()
        score_candidates([a], {"latency": .5, "cost": .3, "memory": .2})
        self.assertEqual(a.effective_weights, {"latency": .625, "cost": .37499999999999994})
        b = score_candidate("B", cost=None)
        self.assertEqual(score_candidates([b], {"cost": 1}, required_metrics={"cost"}), [])
        self.assertIn("required metric cost", b.exclusion_reasons[0])

    def test_deterministic_ties_and_confidence(self):
        a, b = score_candidate("A"), score_candidate("B")
        rows = score_candidates([b, a], {"latency": 1})
        self.assertEqual(rows[0].candidate.id, "A")
        self.assertEqual(assess_confidence(rows, {"latency": 1}), ("LOW", 0))
        a, b = score_candidate("A", 100), score_candidate("B", 200)
        rows = score_candidates([b, a], {"latency": 1})
        self.assertEqual(assess_confidence(rows, {"latency": 1}), ("HIGH", 1))
        a.measurement.successful_runs = 3
        self.assertEqual(assess_confidence(rows, {"latency": 1})[0], "MEDIUM")
        a.measurement.successful_runs = 1
        self.assertEqual(assess_confidence(rows, {"latency": 1})[0], "LOW")
