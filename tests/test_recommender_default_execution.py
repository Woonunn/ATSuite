import tempfile
import unittest
from pathlib import Path
from atsuite.execution.store import JobStore
from atsuite.recommender.service import plan,approve,enqueue,run
from atsuite.recommender.contracts import ExperimentSpec
from tests.test_recommender_candidates import make_config

class DefaultExecution(unittest.TestCase):
    def test_default_factory_exists_and_has_real_stages(self):
        from atsuite.execution.default_handlers import create_handlers
        self.assertTrue(callable(create_handlers))
    def test_missing_approval_cannot_be_replaced_by_request_field(self):
        store=JobStore()
        with self.assertRaises(ValueError):
            enqueue(store,{'spec':{'authorization':'approved_execution'}},{},tenant='t',project='p',subject='s')
        store.close()

    def _experiment(self,root,*,backend=None,oracle=None,count=3,candidates=2):
        import json
        config=make_config(root/'bench');raw=json.loads(config.read_text())
        raw['nodes'][0]['state']={'mode':'stateless'}
        raw['correctness']={'oracle':oracle or {'type':'exact','expected_output':{'answer':42}}}
        config.write_text(json.dumps(raw))
        spec=ExperimentSpec.from_dict({'benchmark':{'config':str(config)},'providers':['aws_lambda'],'regions':{'aws_lambda':'us-east-1'},
            'objective':{'type':'min_cost'},'search':{'max_candidates':candidates},'protocol':{'warmup_runs':1,'exploration_runs':1,'confirmation_runs':count},
            'budget':{'max_estimated_spend':5,'max_wall_time_seconds':120,'max_llm_tokens':0},'advisor':{'enabled':False}})
        frozen=plan(spec,root/'plan'/'plan.json');store=JobStore(root/'jobs.sqlite')
        approval=approve(store,frozen,subject='s',tenant='t',project='p')
        record=enqueue(store,frozen,approval,subject='s',tenant='t',project='p')
        return store,record
    def test_default_chain_executes_workflow_oracle_ledger_and_cleanup(self):
        from tests.fake_execution_backend import FakeExecutionBackend
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);backend=FakeExecutionBackend();store,record=self._experiment(root)
            result=run(store,record['id'],backend=backend)
            self.assertEqual(result['state'],'COMPLETED',result.get('error'))
            self.assertEqual(result['cleanup_status'],'success');self.assertIsNone(result['recommendation']['best_verified'])
            self.assertEqual(result['comparison']['evidence_quality'],'simulated')
            self.assertEqual(len(result['trials']),10)
            self.assertTrue(all(t['output_validation']=='passed' for t in result['trials']))
            self.assertTrue(all(g['eligible'] for g in result['recommendation']['eligibility']))
            self.assertEqual(len([c for c in backend.calls if isinstance(c,tuple) and c[0]=='build']),1)
            self.assertTrue(Path(result['usage_ledger']).is_file());self.assertTrue(Path(result['report_path']).is_file())
            self.assertTrue(all(r['cleanup_status']=='success' for r in result['resource_records'].values()))
            self.assertEqual(set(result['completed_stages']),{'PREFLIGHT','BUILDING','DEPLOYING','EXPLORING','CONFIRMING','ANALYZING'})
            store.close()
    def test_publication_failure_never_deploys(self):
        from tests.fake_execution_backend import FakeExecutionBackend
        with tempfile.TemporaryDirectory() as tmp:
            backend=FakeExecutionBackend(fail_publish=True);store,record=self._experiment(Path(tmp))
            result=run(store,record['id'],backend=backend)
            self.assertEqual(result['state'],'FAILED');self.assertIn('registry denied',result['error'])
            self.assertFalse(any(isinstance(c,tuple) and c[0]=='deploy' for c in backend.calls));store.close()
    def test_bad_output_and_state_are_excluded_without_discarding_trials(self):
        from tests.fake_execution_backend import FakeExecutionBackend
        with tempfile.TemporaryDirectory() as tmp:
            store,record=self._experiment(Path(tmp),oracle={'type':'exact','expected_output':{'answer':999},'expected_state':{'counter':1}})
            result=run(store,record['id'],backend=FakeExecutionBackend())
            self.assertEqual(result['state'],'COMPLETED');self.assertEqual(len(result['trials']),10)
            self.assertIsNone(result['recommendation']['best_verified'])
            for g in result['recommendation']['eligibility']:
                self.assertIn('correctness_failed',g['blocking_reasons']);self.assertIn('state_validation_failed',g['blocking_reasons'])
            store.close()
    def test_cleanup_failure_resume_retries_only_cleanup(self):
        from tests.fake_execution_backend import FakeExecutionBackend
        from atsuite.recommender.service import resume
        with tempfile.TemporaryDirectory() as tmp:
            backend=FakeExecutionBackend(fail_cleanup=True);store,record=self._experiment(Path(tmp))
            failed=run(store,record['id'],backend=backend)
            self.assertEqual(failed['state'],'FAILED');self.assertEqual(failed['cleanup_status'],'failed')
            trials=len(failed['trials']);calls=len(backend.calls)
            resume(store,record['id']);backend.fail_cleanup=False
            result=run(store,record['id'],backend=backend)
            self.assertEqual(result['state'],'COMPLETED');self.assertEqual(len(result['trials']),trials)
            self.assertTrue(all(isinstance(c,tuple) and c[0]=='cleanup' for c in backend.calls[calls:]));store.close()
    def test_frozen_bundle_changes_are_rejected_before_build(self):
        from tests.fake_execution_backend import FakeExecutionBackend
        with tempfile.TemporaryDirectory() as tmp:
            backend=FakeExecutionBackend();store,record=self._experiment(Path(tmp))
            candidate=record['plan']['candidates'][0]
            Path(candidate['bundle_root'],'nodes','tool','tamper.txt').write_text('unfrozen extra input')
            # A tracked file drift is caught even if requested resources stay identical.
            Path(candidate['source_config']).write_text('{}')
            result=run(store,record['id'],backend=backend)
            self.assertEqual(result['state'],'FAILED');self.assertIn('frozen_bundle_changed',result['error']);self.assertEqual(backend.calls,[]);store.close()
    def test_untracked_bundle_injection_is_rejected(self):
        from tests.fake_execution_backend import FakeExecutionBackend
        with tempfile.TemporaryDirectory() as tmp:
            backend=FakeExecutionBackend();store,record=self._experiment(Path(tmp))
            Path(record['plan']['candidates'][0]['bundle_root'],'nodes','tool','injected.py').write_text('print(1)')
            result=run(store,record['id'],backend=backend)
            self.assertEqual(result['state'],'FAILED');self.assertIn('frozen_bundle_changed',result['error'])
            self.assertEqual(backend.calls,[]);store.close()
    def test_export_offline_compare_replays_same_protocol_and_simulation_label(self):
        import json
        from tests.fake_execution_backend import FakeExecutionBackend
        from atsuite.recommender.service import compare_manifest
        with tempfile.TemporaryDirectory() as tmp:
            store,record=self._experiment(Path(tmp));result=run(store,record['id'],backend=FakeExecutionBackend())
            report=json.loads(Path(result['report_path']).read_text())
            offline=compare_manifest(report)
            self.assertEqual(offline['status'],result['recommendation']['status'])
            self.assertEqual(offline.get('best_simulated'),result['recommendation'].get('best_simulated'))
            self.assertIsNone(offline['best_verified']);store.close()
    def test_real_cli_queues_and_worker_uses_default_handlers_without_plugin(self):
        import contextlib,io,json
        from unittest.mock import patch
        from atsuite.cli.recommend import main as recommend_main
        from atsuite.cli.worker import main as worker_main
        from tests.fake_execution_backend import FakeExecutionBackend
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store,original=self._experiment(root);store.close()
            backend=FakeExecutionBackend();stream=io.StringIO()
            with contextlib.redirect_stdout(stream):
                self.assertEqual(recommend_main(['run','--plan',str(root/'plan'/'plan.json'),'--store',str(root/'cli.sqlite'),
                    '--approve-budget','--account','aws_lambda=simulated-account']),0)
            queued=json.loads(stream.getvalue());self.assertEqual(queued['status'],'execution_queued')
            # External cloud/registry/price boundary only; CLI/service/stages/invoker/gate are real.
            with patch('atsuite.execution.cloud_backend.CloudBackend',return_value=backend),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(worker_main(['--store',str(root/'cli.sqlite'),'--experiment',queued['experiment']]),0)
            store=JobStore(root/'cli.sqlite');result=store.get(queued['experiment'])
            self.assertEqual(result['state'],'COMPLETED');self.assertEqual(len(result['trials']),10)
            self.assertIsNone(result['recommendation']['best_verified'])
            self.assertEqual(result['comparison']['evidence_quality'],'simulated');store.close()
    def test_partial_deployment_resume_recreates_deleted_endpoints(self):
        from tests.fake_execution_backend import FakeExecutionBackend
        from atsuite.recommender.service import resume
        class LifetimeBackend(FakeExecutionBackend):
            def __init__(self):super().__init__();self.live=set();self.count=0
            def deploy(self,candidate,published,record,intent_callback):
                self.count+=1
                if self.count==2:raise RuntimeError('deployment interrupted')
                value=super().deploy(candidate,published,record,intent_callback)
                self.live.add(candidate['candidate_id']);return value
            def cleanup(self,resource,record):
                self.live.discard(resource['candidate_id']);return super().cleanup(resource,record)
            def invoke(self,candidate,*args):
                if candidate['candidate_id'] not in self.live:raise RuntimeError('deleted endpoint reused')
                return super().invoke(candidate,*args)
        with tempfile.TemporaryDirectory() as tmp:
            backend=LifetimeBackend();store,record=self._experiment(Path(tmp))
            failed=run(store,record['id'],backend=backend)
            self.assertEqual(failed['state'],'FAILED');self.assertEqual(failed['cleanup_status'],'success')
            self.assertEqual(backend.live,set());resume(store,record['id'])
            result=run(store,record['id'],backend=backend)
            self.assertEqual(result['state'],'COMPLETED',result.get('error'))
            self.assertEqual(len(result['trials']),10);self.assertEqual(backend.live,set())
            self.assertEqual(len([c for c in backend.calls if isinstance(c,tuple) and c[0]=='build']),1)
            store.close()
