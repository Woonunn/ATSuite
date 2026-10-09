import tempfile
import unittest
from pathlib import Path
from atsuite.execution.store import JobStore
from atsuite.execution.jobs import ExperimentJob

class DurableJobs(unittest.TestCase):
    def test_asynchronous_delete_remains_queued_until_readback(self):
        store=JobStore();store.create('e',{'cleanup_terminal_state':'COMPLETED','cleanup_required':True,
            'completed_stages':['BUILDING','DEPLOYING','EXPLORING','CONFIRMING','ANALYZING']},state='CLEANUP_PENDING')
        calls=[]
        def cleanup(record):
            calls.append(record['state'])
            return {'cleanup_status':'pending' if len(calls)==1 else 'success','readback':'deleting' if len(calls)==1 else 'not_found'}
        first=ExperimentJob(store,'e',{'CLEANUP':cleanup}).run()
        self.assertEqual(first['state'],'CLEANUP_PENDING')
        self.assertEqual(first['cleanup_status'],'pending')
        self.assertIn('e',store.queued())
        second=ExperimentJob(store,'e',{'CLEANUP':cleanup}).run()
        self.assertEqual(second['state'],'COMPLETED')
        self.assertEqual(second['readback'],'not_found')
        self.assertEqual(calls,['CLEANUP_PENDING','CLEANUP_PENDING']);store.close()
    def test_two_connections_cannot_claim_same_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=JobStore(Path(tmp)/'jobs.sqlite'); b=JobStore(Path(tmp)/'jobs.sqlite')
            a.create('e',{},state='APPROVED')
            token=a.claim('e','a')
            self.assertTrue(token)
            self.assertIsNone(b.claim('e','b'))
            with self.assertRaises(RuntimeError): b.checkpoint('e','BUILDING',{},lease_token='stale')
            a.release('e',token); self.assertTrue(b.claim('e','b'))
            a.close(); b.close()
    def test_resume_never_repeats_successful_stage(self):
        store=JobStore(); store.create('e',{'completed_stages':['BUILDING'],'stage_records':{'BUILDING':{'status':'succeeded'}}},state='DEPLOYING')
        called=[]
        handlers={stage:(lambda record,s=stage: (called.append(s) or {'done':True})) for stage in ('BUILDING','DEPLOYING','EXPLORING','CONFIRMING','ANALYZING')}
        result=ExperimentJob(store,'e',handlers).run()
        self.assertEqual(result['state'],'COMPLETED'); self.assertNotIn('BUILDING',called)
        self.assertEqual(result['stage_records']['DEPLOYING']['status'],'succeeded')
        store.close()
