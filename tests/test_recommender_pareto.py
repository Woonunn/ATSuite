import unittest

from atsuite.recommender.model import MetricValue
from atsuite.recommender.pareto import pareto_frontier
from tests.test_recommender_constraints import score_candidate


class ParetoTests(unittest.TestCase):
    def test_tradeoffs_and_dominance(self):
        candidates = [score_candidate("A", 1, 10), score_candidate("B", 2, 8), score_candidate("C", 3, 12)]
        self.assertEqual(pareto_frontier(candidates, ["latency", "cost"]), ["A", "B"])
        self.assertEqual(pareto_frontier([], ["cost"]), [])

    def test_ties_and_missing_evidence_are_not_dominance(self):
        a, b = score_candidate("A"), score_candidate("B")
        self.assertEqual(pareto_frontier([a, b]), ["A", "B"])
        b.metrics["cost"] = MetricValue()
        b.metrics["latency"] = MetricValue(1, "available")
        self.assertEqual(pareto_frontier([a, b]), ["A", "B"])
