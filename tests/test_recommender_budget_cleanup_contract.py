import tempfile
import unittest
from pathlib import Path
from atsuite.execution.store import JobStore
from atsuite.execution.budget import BudgetController
from atsuite.execution.cleanup import reconcile_expired

class BudgetCleanupContract(unittest.TestCase):
    def record(self,store):
        store.create('e',{'plan':{'spec':{'search':{'max_candidates':1},'protocol':{'warmup_runs':0,'exploration_runs':0,'confirmation_runs':2},'budget':{'currency':'USD','max_estimated_spend':10,'max_wall_time_seconds':120,'include_cleanup_reserve':True}}}},state='APPROVED')
    def test_trial_and_candidate_quota_before_dispatch(self):
        store=JobStore();self.record(store);budget=BudgetController(store,'e')
        budget.reserve(reservation_id='a',trials=2,candidates=1,spend=1)
        with self.assertRaisesRegex(RuntimeError,'trial budget'):budget.reserve(reservation_id='b',trials=1)
        with self.assertRaisesRegex(RuntimeError,'candidate budget'):budget.reserve(reservation_id='c',candidates=1)
        store.close()
    def test_durable_reservation_and_cleanup_reserve(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'j.sqlite';store=JobStore(path);self.record(store)
            BudgetController(store,'e').reserve(reservation_id='dispatch',spend=9)
            store.close();store=JobStore(path);budget=BudgetController(store,'e')
            budget.reserve(reservation_id='dispatch',spend=9)
            with self.assertRaises(RuntimeError):budget.reserve(reservation_id='extra',spend=.1)
            budget.reserve(reservation_id='clean',spend=1,cleanup=True)
            self.assertEqual(len(store.get('e')['budget_ledger']['reservations']),2)
            store.close()
    def test_ttl_reconciler_cannot_delete_under_live_lease(self):
        class Backend:
            def __init__(self):self.calls=[]
            def cleanup(self,resource,record):self.calls.append(resource);return {'cleanup_status':'success'}
        store=JobStore();self.record(store);backend=Backend()
        store.checkpoint('e','DEPLOYING',{'resource_records':{'r':{'expires_at':1}}})
        token=store.claim('e','worker');self.assertEqual(reconcile_expired(store,now=2,backend=backend),[])
        self.assertEqual(backend.calls,[]);store.release('e',token)
        self.assertEqual(len(reconcile_expired(store,now=2,backend=backend)),1)
        self.assertEqual(store.get('e')['resource_records']['r']['cleanup_status'],'success');store.close()
