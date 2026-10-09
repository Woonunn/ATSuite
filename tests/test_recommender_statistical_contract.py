import unittest
from atsuite.recommender.contracts import ObjectiveSpec, ProtocolSpec
from atsuite.recommender.decision import decide
from tests.recommender_fixtures import verified_candidate

class StatisticalContract(unittest.TestCase):
    def test_balanced_ranking_includes_failure_rate_uncertainty(self):
        second=verified_candidate('b')
        second['trials'][0].update(status='timeout',output_validation='unknown')
        from atsuite.recommender.evidence_adapter import candidate_evidence
        second=candidate_evidence(second,second['trials'],configuration={'status':'verified','image_digest':second['image_digest'],
            'effective_image_digest':second['image_digest'],'state_validation':'passed'},
            cost={'amount':.1*299,'currency':'USD','coverage':'complete'},ledger=[{'quantity':'1'}],
            rate_snapshot={'snapshot_id':'rate'},oracle_version='oracle-v1',scenario_id='s1')
        second['metrics']['cost'].update(ci_lower=.1,ci_upper=.1)
        result=decide([verified_candidate('a'),second],ObjectiveSpec(type='balanced'),minimum_score_margin=.0001)
        self.assertEqual(result.status,'no_clear_winner')
        self.assertIsNone(result.best_verified)
        self.assertTrue(all(g['eligible'] for g in result.eligibility))
    def test_latency_objective_has_a_declared_success_floor(self):
        objective=ObjectiveSpec.from_dict({'type':'min_latency'})
        self.assertEqual(objective.min_success_rate,.95)
        first=verified_candidate('a',duration=1)
        trials=first['trials']
        for trial in trials[:150]:
            trial.update(status='timeout',output_validation='unknown')
        from atsuite.recommender.evidence_adapter import candidate_evidence
        first=candidate_evidence(first,trials,configuration={'status':'verified','image_digest':first['image_digest'],
            'effective_image_digest':first['image_digest'],'state_validation':'passed'},oracle_version='oracle-v1',scenario_id='s1')
        result=decide([first,verified_candidate('b',duration=100)],objective)
        self.assertIsNone(result.best_verified)
        self.assertIn('success_rate_lower_bound_insufficient',result.excluded[0]['reasons'])
        self.assertEqual(len(first['trials']),300)
    def test_small_fast_sample_cannot_prove_p95_slo(self):
        result=decide([verified_candidate('a',count=3),verified_candidate('b',count=3,cost=.3)],
            ObjectiveSpec(type='min_cost',max_p95_workflow_ms=1000),protocol=ProtocolSpec(confirmation_runs=3))
        self.assertIsNone(result.best_verified)
        self.assertIn('deadline_lower_bound_insufficient',result.excluded[0]['reasons'])
    def test_small_sample_cannot_prove_latency_winner(self):
        result=decide([verified_candidate('a',count=3),verified_candidate('b',count=3,duration=1000)],
            ObjectiveSpec(type='min_latency'),protocol=ProtocolSpec(confirmation_runs=3))
        self.assertIsNone(result.best_verified)
        self.assertIn('quantile_uncertainty_insufficient',result.excluded[0]['reasons'])
    def test_configuration_must_be_content_bound(self):
        bad=verified_candidate('a');bad['configuration_ref']='unresolvable'
        result=decide([bad,verified_candidate('b')],ObjectiveSpec(type='min_cost'))
        self.assertIn('configuration_reference_invalid',result.excluded[0]['reasons'])
    def test_forged_p95_cannot_replace_observed_distribution(self):
        bad=verified_candidate('a',duration=1000);bad['metrics']['p95_workflow_ms']['value']=1
        result=decide([bad,verified_candidate('b',duration=500)],ObjectiveSpec(type='min_latency'))
        self.assertIn('quantile_value_mismatch',result.excluded[0]['reasons'])
    def test_large_independent_blocks_can_produce_latency_winner(self):
        result=decide([verified_candidate('a',duration=100),verified_candidate('b',duration=1000)],ObjectiveSpec(type='min_latency'))
        self.assertEqual(result.best_verified,'a')
