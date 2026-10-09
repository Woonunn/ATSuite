import copy
import unittest
from atsuite.recommender.contracts import EvidenceMetric, ObjectiveSpec,ProtocolSpec
from atsuite.recommender.decision import decide
from tests.recommender_fixtures import verified_candidate

class EvidenceGates(unittest.TestCase):
    def test_real_module_and_two_valid_candidates(self):
        from pathlib import Path
        import atsuite.recommender.decision as module
        self.assertEqual(Path(module.__file__).resolve().parents[2],Path(__file__).resolve().parents[1])
        result=decide([verified_candidate('a',.00001),verified_candidate('b',.00002)],ObjectiveSpec(type='min_cost'))
        self.assertEqual((result.status,result.best_verified),('recommended','a'))
    def test_tie_and_derived_metrics(self):
        result=decide([verified_candidate('a'),verified_candidate('b')],ObjectiveSpec(type='min_cost'))
        self.assertEqual(result.status,'no_clear_winner')
    def test_metric_source_must_match_confirmed_measurement_source(self):
        for source in ('predicted', 'simulated', 'manual_upload', 'unknown'):
            with self.subTest(source=source):
                bad=verified_candidate('bad',.00001)
                bad['metrics']['cost']['source']=source
                result=decide([bad,verified_candidate('good',.00002)],ObjectiveSpec(type='min_cost'))
                self.assertIsNone(result.best_verified)
                self.assertIn('metric_source_mismatch:cost',result.excluded[0]['reasons'])
    def test_all_negatives_reason_codes(self):
        for mutation,expected in [
            (lambda r:r.update(output_validation='failed'),'correctness_failed'),
            (lambda r:r['metrics'].pop('p95_workflow_ms'),'missing_metric:p95_workflow_ms'),
            (lambda r:r['metrics']['cost'].update(unit='CNY'),'currency_mismatch'),
            (lambda r:r.update(effective_image_digest='wrong'),'image_digest_mismatch'),
            (lambda r:r['metrics']['cost'].update(evidence_refs=['fake']),'unresolved_or_corrupt_reference'),
            (lambda r:r.update(phase='exploration'),'confirmation_missing'),
            (lambda r:r.update(cost_scope='other'),'cost_scope_mismatch'),
            (lambda r:r['metrics']['failure_rate'].update(sample_count=0),'zero_samples:failure_rate'),
            (lambda r:r['trials'][0].update(status='timeout'),'failure_rate_count_mismatch'),
            (lambda r:r.update(measurement_source='simulated'),'measurement_source_unverified')]:
            with self.subTest(expected=expected):
                bad=verified_candidate('bad'); mutation(bad)
                result=decide([bad,verified_candidate('good',.9)],ObjectiveSpec(type='min_cost',max_p95_workflow_ms=1000))
                self.assertIsNone(result.best_verified)
                self.assertIn(expected,result.excluded[0]['reasons'])
    def test_insufficient_counts_and_bound(self):
        result=decide([verified_candidate('a',count=20),verified_candidate('b',count=20)],
            ObjectiveSpec(type='min_cost',min_success_rate=.99),protocol=ProtocolSpec(confirmation_runs=20))
        self.assertIn('success_rate_lower_bound_insufficient',result.excluded[0]['reasons'])
        full=decide([verified_candidate('a'),verified_candidate('b',.3)],ObjectiveSpec(type='min_cost',min_success_rate=.99))
        self.assertIsNone(full.best_verified)  # 300 is insufficient after simultaneous coverage adjustment.
        full=decide([verified_candidate('a',count=500),verified_candidate('b',.3,count=500)],
            ObjectiveSpec(type='min_cost',min_success_rate=.99),protocol=ProtocolSpec(confirmation_runs=500))
        self.assertEqual(full.best_verified,'a')
    def test_malformed_counts_and_duplicate_ids(self):
        for n in (True,False,float('nan'),'300',-1):
            with self.assertRaises(ValueError): EvidenceMetric('x',1,'ms','workflow',n)
        with self.assertRaises(ValueError): decide([verified_candidate('a'),verified_candidate('a')],ObjectiveSpec())
    def test_partial_scope_not_no_feasible(self):
        result=decide([verified_candidate('a',duration=10000),{'candidate_id':'unmeasured'}],ObjectiveSpec(max_p95_workflow_ms=1000))
        self.assertEqual(result.status,'insufficient_evidence')
