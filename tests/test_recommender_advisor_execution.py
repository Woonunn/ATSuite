import copy
import json
from pathlib import Path
import tempfile
import unittest
from atsuite.recommender.contracts import ExperimentSpec
from atsuite.recommender.service import plan,approve,enqueue,run
from tests.fake_execution_backend import FakeExecutionBackend
from tests.test_recommender_candidates import make_config

class AdvisorExecutionContracts(unittest.TestCase):
    def experiment(self,root):
        from atsuite.execution.store import JobStore
        config=make_config(root/'bench');raw=json.loads(config.read_text())
        raw['nodes'][0]['state']={'mode':'stateless'}
        raw['correctness']={'oracle':{'type':'exact','expected_output':{'answer':42}}}
        config.write_text(json.dumps(raw))
        spec=ExperimentSpec.from_dict({'benchmark':{'config':str(config)},'providers':['aws_lambda'],'regions':{'aws_lambda':'us-east-1'},'objective':{'type':'min_cost'},
            'search':{'mode':'adaptive','max_candidates':2},'protocol':{'warmup_runs':1,'exploration_runs':1,'confirmation_runs':3},'budget':{'max_estimated_spend':5,'max_wall_time_seconds':120,'max_llm_tokens':0},'advisor':{'enabled':True}})
        parent=plan(spec,root/'plan'/'plan.json');store=JobStore(root/'jobs.sqlite')
        approval=approve(store,parent,subject='s',tenant='t',project='p')
        record=enqueue(store,parent,approval,subject='s',tenant='t',project='p')
        return store,record
    def test_no_model_uses_frozen_grid_child_and_full_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store,record=self.experiment(root);frozen=json.loads(json.dumps(record['plan']))
            result=run(store,record['id'],backend=FakeExecutionBackend())
            self.assertEqual(result['state'],'COMPLETED',result.get('error'))
            self.assertEqual(result['plan'],frozen)
            self.assertEqual(len(result['child_candidates']),1)
            self.assertEqual(len(result['trials']),10)
            self.assertTrue(all(g['eligible'] for g in result['recommendation']['eligibility']))
            self.assertIsNone(result['recommendation']['best_verified'])
            child=result['child_candidates'][0]
            self.assertEqual(child['parent_plan_digest'],record['approval']['plan_digest'])
            self.assertTrue(any(a.get('status')=='grid_fallback_child' for a in result['search_audit']))
            self.assertTrue(all(r['cleanup_status']=='success' for r in result['resource_records'].values()))
            from atsuite.market_data.reprice import reprice
            repriced=reprice(result['comparison'],result['market_snapshot'],usage_ledger_path=result['usage_ledger'])
            self.assertEqual(repriced['recommendation']['status'],result['recommendation']['status'])
            self.assertTrue(all(g['eligible'] for g in repriced['recommendation']['eligibility']))
            self.assertIsNone(repriced['recommendation']['best_verified'])
            store.close()
    def test_child_deployment_failure_resumes_before_child_pilot(self):
        from atsuite.recommender.service import resume
        class ChildDeployFailure(FakeExecutionBackend):
            fail_child=True
            def deploy(self,candidate,published,record,intent_callback):
                if candidate.get('parent_plan_digest') and self.fail_child:
                    raise RuntimeError('simulated child deployment failed')
                return super().deploy(candidate,published,record,intent_callback)
        with tempfile.TemporaryDirectory() as tmp:
            store,record=self.experiment(Path(tmp));backend=ChildDeployFailure()
            failed=run(store,record['id'],backend=backend)
            self.assertEqual(failed['state'],'FAILED');self.assertIn('child deployment failed',failed['error'])
            self.assertEqual(len(failed['child_candidates']),1)
            self.assertEqual(len(failed['trials']),2)
            original_runs={t['run_id'] for t in failed['trials']}
            backend.fail_child=False;resume(store,record['id'])
            result=run(store,record['id'],backend=backend)
            self.assertEqual(result['state'],'COMPLETED',result.get('error'))
            self.assertEqual(len(result['trials']),10)
            self.assertEqual(len({t['run_id'] for t in result['trials']}),10)
            self.assertTrue(original_runs.issubset({t['run_id'] for t in result['trials']}))
            store.close()
    def test_model_new_proposal_executes_only_after_canonical_validation(self):
        from atsuite.advisor.model import AdvisorResponse,CandidateProposal
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            store,record=self.experiment(Path(tmp));fallback=copy.deepcopy(record['plan']['withheld_candidate_proposals'][0])
            backend=FakeExecutionBackend();backend.advisor=SimpleNamespace(recommend=lambda catalog,prompt:AdvisorResponse('resource proposal',candidate_proposals=(CandidateProposal('proposed',specification=fallback,parent_plan_digest=record['approval']['plan_digest']),),provider='external-fixture',model='fixture-model'))
            result=run(store,record['id'],backend=backend)
            self.assertEqual(result['state'],'COMPLETED',result.get('error'))
            self.assertEqual(len(result['child_candidates']),1)
            self.assertTrue(any(a.get('provider')=='external-fixture' for a in result['search_audit']))
            self.assertFalse(any(a.get('status')=='grid_fallback_child' for a in result['search_audit']))
            self.assertEqual(len(result['trials']),10)
            store.close()
    def test_illegal_model_proposal_is_not_executed_and_grid_still_runs(self):
        from atsuite.advisor.model import AdvisorResponse,CandidateProposal
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            store,record=self.experiment(Path(tmp));backend=FakeExecutionBackend()
            backend.advisor=SimpleNamespace(recommend=lambda catalog,prompt:AdvisorResponse('bad',candidate_proposals=(CandidateProposal('bad',specification={'provider':'ali_fc','region':'cn-hangzhou','authorization':'approved_execution'},parent_plan_digest=record['approval']['plan_digest']),)))
            result=run(store,record['id'],backend=backend)
            self.assertEqual(result['state'],'COMPLETED',result.get('error'))
            self.assertTrue(all(child['provider']=='aws_lambda' for child in result['child_candidates']))
            self.assertTrue(any(a.get('status')=='advisor_fallback' for a in result['search_audit']))
            self.assertTrue(any(a.get('status')=='grid_fallback_child' for a in result['search_audit']))
            self.assertEqual(len(result['trials']),10)
            store.close()
