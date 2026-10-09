import unittest
from atsuite.prediction.features import candidate_features
class PredictionRegressions(unittest.TestCase):
    def test_mapping_candidates_keep_resources(self):
        self.assertEqual(candidate_features({'resources_by_target': {'x': {'cpu':2}}})['total_cpu'],2)
    def test_provider_features_differ(self):
        self.assertNotEqual(candidate_features({'provider':'aws_lambda'}), candidate_features({'provider':'ali_fc'}))

class AcquisitionContracts(unittest.TestCase):
    def candidates(self):
        return [{'candidate_id':key,'provider':'aws_lambda','family':'faas'} for key in ('pilot','slow','fast')]
    def predictor(self,ood=False,raises=False):
        from types import SimpleNamespace
        from atsuite.prediction.predictor import Prediction
        class FakeExternalModel:
            model=SimpleNamespace(quality='approved_local_contract')
            def predict(self,workload,candidate,pilot=None):
                if raises: raise RuntimeError('API unavailable')
                return Prediction({'latency_ms':1 if candidate['candidate_id']=='fast' else 20},'predicted','fixture-model',ood=ood)
        return FakeExternalModel()
    def test_prediction_changes_next_pick_and_is_audited(self):
        from atsuite.prediction.acquisition import select_next_candidates
        c=self.candidates()
        grid=select_next_candidates({},c,observed_ids=['pilot'])
        guided=select_next_candidates({},c,predictor=self.predictor(),observed_ids=['pilot'])
        self.assertEqual(grid.candidates[0]['candidate_id'],'slow')
        self.assertEqual(guided.candidates[0]['candidate_id'],'fast')
        self.assertEqual(guided.audit[0]['reason'],'predicted_acquisition')
        self.assertEqual(guided.audit[0]['model_quality'],'approved_local_contract')
    def test_ood_and_error_really_use_grid(self):
        from atsuite.prediction.acquisition import select_next_candidates
        for p in (self.predictor(ood=True),self.predictor(raises=True)):
            result=select_next_candidates({},self.candidates(),predictor=p,observed_ids=['pilot'])
            self.assertEqual(result.mode,'grid_fallback')
            self.assertEqual(result.candidates[0]['candidate_id'],'slow')
    def test_competing_provider_coverage_precedes_prediction(self):
        from atsuite.prediction.acquisition import select_next_candidates
        candidates=self.candidates()+[{'candidate_id':'ali','provider':'ali_fc','family':'faas'}]
        result=select_next_candidates({},candidates,predictor=self.predictor(),observed_ids=['pilot'])
        self.assertEqual(result.candidates[0]['candidate_id'],'ali')
        self.assertEqual(result.audit[0]['reason'],'provider_family_coverage')
    def test_fresh_observed_pilot_is_numeric_input_to_next_prediction(self):
        from types import SimpleNamespace
        from atsuite.prediction.predictor import Prediction
        from atsuite.prediction.acquisition import select_next_candidates
        seen=[]
        class PilotModel:
            model=SimpleNamespace(quality='approved_local_contract',training_data_digest='frozen-data')
            def predict(self,workload,candidate,pilot=None):
                seen.append(pilot)
                return Prediction({'latency_ms':pilot['latency_ms'] if candidate['candidate_id']=='fast' else pilot['latency_ms']+10},'predicted','pilot-model')
        selected=select_next_candidates({},self.candidates(),predictor=PilotModel(),pilot={'pilot':{'latency_ms':37,'failure_rate':.1}},observed_ids=['pilot'])
        self.assertEqual(selected.candidates[0]['candidate_id'],'fast')
        self.assertTrue(all(p['latency_ms']==37 and p['failure_rate']==.1 for p in seen))
        self.assertEqual(selected.audit[0]['training_data_digest'],'frozen-data')
    def test_family_and_time_splits_do_not_leak(self):
        from atsuite.prediction.dataset import Dataset,DatasetRow
        d=Dataset([DatasetRow('w1','a','s','2026-01-01',{}, {},workload_family='travel'),DatasetRow('w2','b','s','2026-03-01',{}, {},workload_family='travel'),DatasetRow('w3','c','s','2026-01-01',{}, {},workload_family='science')])
        train,test=d.split(cutoff='2026-02-01')
        self.assertEqual({r.workload_family for r in train.rows},{'science'})
        self.assertEqual({r.workload_family for r in test.rows},{'travel'})
    def test_synthetic_evaluation_never_marks_real_quality(self):
        from atsuite.prediction.dataset import Dataset,DatasetRow
        from atsuite.prediction.train import train_model
        from atsuite.prediction.predictor import Predictor
        from atsuite.prediction.evaluate import evaluate
        dataset=Dataset([DatasetRow('w','a','s','2026-01-01',{'x':1},{'latency_ms':1})])
        report=evaluate(Predictor(train_model(dataset)),dataset)
        self.assertEqual(report['model_quality'],'insufficient_real_data')
    def test_model_registry_versions_are_immutable_and_cannot_escape(self):
        from atsuite.prediction.registry import ModelRegistry
        from atsuite.prediction.train import ModelArtifact
        from dataclasses import replace
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            registry=ModelRegistry(root);model=ModelArtifact('model','linear_ridge',(),(),{}, {},1,1)
            original=registry.register(model).read_text()
            with self.assertRaises(ValueError):registry.register(replace(model,training_data_digest='changed'))
            self.assertEqual(registry.load('model').training_data_digest,'')
            self.assertEqual(registry.register(model).read_text(),original)
            with self.assertRaises(ValueError):registry.register(replace(model,model_id='../escape'))
