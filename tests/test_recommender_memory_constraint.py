import unittest
from tests.recommender_fixtures import verified_candidate
from atsuite.recommender.contracts import ObjectiveSpec
from atsuite.recommender.decision import decide

class MemoryConstraints(unittest.TestCase):
    def test_observed_memory_constraint_can_pass(self):
        result=decide([verified_candidate('a',memory=128),verified_candidate('b',cost=.3,memory=256)],ObjectiveSpec(type='min_cost',max_memory_mb=512))
        self.assertEqual(result.best_verified,'a')
    def test_observed_violation_and_missing_are_distinct(self):
        result=decide([verified_candidate('a',memory=1024),verified_candidate('b',cost=.3)],ObjectiveSpec(type='min_cost',max_memory_mb=512))
        self.assertIsNone(result.best_verified)
        self.assertIn('memory_constraint_violation',result.excluded[0]['reasons'])
        self.assertIn('missing_metric:memory_mb',result.excluded[1]['reasons'])
