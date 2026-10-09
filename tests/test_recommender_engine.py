import json
import tempfile
import unittest
from pathlib import Path

from atsuite.recommender.engine import RecommendationEngine
from atsuite.recommender.metrics import aggregate_runs
from atsuite.recommender.model import RecommendationConstraints
from atsuite.recommender.normalize import CurrencyNormalizer
from tests.test_recommender_candidates import make_config
from tests.test_recommender_metrics import run_record


class FakeExperimentRunner:
    def __init__(self, *, fail_provider=None, missing_cost=False):
        self.calls = []
        self.fail_provider = fail_provider
        self.missing_cost = missing_cost

    def run_candidate(self, candidate, *, runs, warmup_runs, uid_prefix):
        self.calls.append(candidate.id)
        if candidate.provider == self.fail_provider:
            raise RuntimeError("Provider deployment failed")
        latency, cost = (2000, .001) if candidate.family == "faas" else (1200, .002)
        records = [run_record(uid=f"{uid_prefix}-{candidate.id}-{index}", latency=latency, cost=cost) for index in range(runs)]
        if self.missing_cost:
            for record in records:
                record["summary"]["metric_availability"]["total_price"] = "unavailable"
        return aggregate_runs(candidate, records, CurrencyNormalizer())


class EngineTests(unittest.TestCase):
    def test_legacy_reports_are_readable_but_never_publish_unverified_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=make_config(Path(tmp));runner=FakeExperimentRunner()
            result=RecommendationEngine(runner,output_dir=Path(tmp)/'out').recommend(config,providers=['aws_lambda','aws_agentcore'])
            self.assertIsNone(result.winner)
            self.assertEqual(result.experiment['canonical_decision']['status'],'insufficient_evidence')
            self.assertEqual(result.experiment['evidence_quality'],'legacy_unverified')
            payload=json.loads(Path(result.report_path).read_text())
            self.assertEqual(len(payload['candidates']),2)
            self.assertEqual(len(payload['candidates'][0]['run_records']),5)
            self.assertEqual(payload['candidates'][1]['deployment_shape']['total_cpu'],2)
            self.assertEqual(payload['pricing']['normalized_currency'],'USD')
    def test_canonical_runner_evidence_uses_shared_winner(self):
        from tests.recommender_fixtures import verified_candidate
        from atsuite.recommender.service import compare
        from atsuite.recommender.contracts import ObjectiveSpec,ProtocolSpec
        class CanonicalRunner(FakeExperimentRunner):
            def __init__(self):super().__init__();self.evidence_candidates=[]
            def run_candidate(self,candidate,**kw):
                row=super().run_candidate(candidate,**kw)
                self.evidence_candidates.append(verified_candidate(candidate.id,.001 if candidate.family=='faas' else .002,
                    count=kw['runs'],duration=2000 if candidate.family=='faas' else 1200))
                return row
        with tempfile.TemporaryDirectory() as tmp:
            config=make_config(Path(tmp))
            for objective,kind,expected in [('cost','min_cost','aws_lambda'),('latency','min_latency','aws_agentcore'),('balanced','balanced','aws_agentcore')]:
                runner=CanonicalRunner(); result=RecommendationEngine(runner,output_dir=Path(tmp)/'out').recommend(config,providers=['aws_lambda','aws_agentcore'],objective=objective,runs=300)
                canonical=compare(runner.evidence_candidates,ObjectiveSpec(type=kind),protocol=ProtocolSpec(confirmation_runs=300))
                self.assertEqual(result.winner.candidate.id,expected)
                self.assertEqual(result.experiment['canonical_decision']['best_verified'],canonical['best_verified'])
    def test_incomplete_and_constraint_evidence_do_not_become_winners(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=make_config(Path(tmp));runner=FakeExperimentRunner(fail_provider='ali_fc')
            result=RecommendationEngine(runner,output_dir=Path(tmp)/'out').recommend(config,providers=['ali_fc','unknown','aws_lambda','aws_agentcore'])
            self.assertIsNone(result.winner);self.assertEqual(len(result.candidates),4);self.assertEqual(len(runner.calls),3)
            constrained=RecommendationEngine(FakeExperimentRunner(),output_dir=Path(tmp)/'out').recommend(config,providers=['aws_lambda','aws_agentcore'],objective='cost',constraints=RecommendationConstraints(max_p95_latency_ms=1500))
            self.assertIsNone(constrained.winner);self.assertIsNone(constrained.candidates[0].score)
            self.assertTrue(any('max_p95_latency_ms' in r for r in constrained.candidates[0].exclusion_reasons))
            self.assertTrue(Path(constrained.report_path).is_file())
    def test_raw_legacy_runs_recompute_descriptive_scores_without_verification(self):
        from atsuite.recommender.metrics import comparison_metrics
        from atsuite.recommender.model import CandidateScore,DeploymentCandidate
        from atsuite.recommender.scoring import score_candidates
        with tempfile.TemporaryDirectory() as tmp:
            config=make_config(Path(tmp));result=RecommendationEngine(FakeExperimentRunner(),output_dir=Path(tmp)/'out').recommend(config)
            payload=json.loads(Path(result.report_path).read_text());rows=[]
            for item in payload['candidates']:
                candidate=DeploymentCandidate(item['id'],item['provider'],item['cloud'],item['family'],Path(item['config_path']))
                measurement=aggregate_runs(candidate,item['run_records'],CurrencyNormalizer(payload['pricing']['fx_rates']))
                rows.append(CandidateScore(measurement,comparison_metrics(measurement,payload['experiment']['latency_stat'])))
            ranked=score_candidates(rows,payload['objective']['weights'])
            self.assertEqual([row.candidate.id for row in ranked],payload['rankings'])
            self.assertIsNone(result.winner)
            for row in payload['candidates']:
                calculated=0
                for metric,weight in row['effective_weights'].items():
                    bound=payload['normalization_bounds'][metric];value=row['comparison_metrics'][metric]['value']
                    calculated+=weight*((bound['max']-value)/(bound['max']-bound['min']) if bound['max']!=bound['min'] else 1)
                self.assertAlmostEqual(calculated,row['score'])
    def test_validation_before_legacy_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=make_config(Path(tmp));runner=FakeExperimentRunner();engine=RecommendationEngine(runner,output_dir=Path(tmp)/'out')
            for kw in ({'runs':1},{'runs':2},{'weights':{'cost':.9}},{'warmup_runs':-1},{'fx_rates':{'USD':2}}):
                before=len(runner.calls)
                with self.assertRaises(ValueError):engine.recommend(config,**kw)
                self.assertEqual(len(runner.calls),before)
            with self.assertRaisesRegex(ValueError,'canonical service approval'):
                RecommendationEngine(output_dir=Path(tmp)/'out').recommend(config)
