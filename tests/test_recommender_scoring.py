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
        self.assertAlmostEqual(a.effective_weights["latency"], .625)
        self.assertAlmostEqual(a.effective_weights["cost"], .375)
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

    def test_tie_break_order_failure_latency_cost_id(self):
        for a, b, expected in (
            (score_candidate("A", 1, 1, .1), score_candidate("B", 10, 10, 0), "B"),
            (score_candidate("A", 20, 1), score_candidate("B", 10, 10), "B"),
            (score_candidate("A", 10, 20), score_candidate("B", 10, 10), "B"),
            (score_candidate("A"), score_candidate("B"), "A"),
        ):
            self.assertEqual(score_candidates([b, a], {"memory": 1})[0].candidate.id, expected)

    def test_partial_metric_is_never_scored(self):
        a = score_candidate("A")
        a.metrics["cost"] = MetricValue(0, "partial")
        score_candidates([a], {"latency": .5, "cost": .5})
        self.assertEqual(a.effective_weights, {"latency": 1})
        self.assertEqual(score_candidates([a], {"cost": 1}, required_metrics={"cost"}), [])

    def test_confidence_variance_missing_metrics_diagnostics_and_margin(self):
        for change, expected in (
            (lambda a: setattr(a.measurement.latency_ms, "cv", .2), "MEDIUM"),
            (lambda a: setattr(a.measurement.latency_ms, "cv", .3), "LOW"),
            (lambda a: a.measurement.diagnostics.append({"kind": "collector_error"}), "LOW"),
        ):
            rows = score_candidates([score_candidate("A", 100), score_candidate("B", 200)], {"latency": 1})
            change(rows[0])
            self.assertEqual(assess_confidence(rows, {"latency": 1})[0], expected)
        rows = score_candidates([score_candidate("A", 100), score_candidate("B", 200)], {"latency": 1})
        rows[0].score = rows[1].score + .006
        self.assertEqual(assess_confidence(rows, {"latency": 1})[0], "LOW")
