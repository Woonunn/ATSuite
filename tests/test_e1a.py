"""Synthetic contract tests; no fixture bill or timing is a cloud measurement."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from experiments.exp1.runner.result_collector import interval_union,parallel_peak,known_sum,request_metrics
from experiments.exp1.runner.static_deployment_runner import reserve_budget
from experiments.exp1.analysis.compare_strategies import compare,reconcile,PROVIDERS
from benchmarks.DataSciBench.nodes.AgentTools.guards import safe_files
from experiments.exp1.data.prepare_e1a import sha


class MetricsTests(unittest.TestCase):
    def test_overlapping_path_is_not_sum_of_parallel_work(self):
        self.assertEqual(interval_union([(0,4),(1,5),(8,9)]),6)
        self.assertEqual(parallel_peak([(0,4),(1,5),(5,8)]),2)
        with self.assertRaises(ValueError):interval_union([(2,1)])

    def test_missing_metrics_remain_unknown_including_failed_request(self):
        self.assertIsNone(known_sum([]));self.assertIsNone(known_sum([None,5]));self.assertEqual(known_sum([0,5]),5)
        with tempfile.TemporaryDirectory() as temporary:
            terminal={'status':'failed','completed_monotonic_ns':30000000,'planned_arrival_monotonic_ns':10000000,
                'submitted_monotonic_ns':11000000,'harness_started_monotonic_ns':12000000,'error':{'type':'Fixture'}}
            metrics=request_metrics({'arrival_id':'a'},terminal,Path(temporary))
            self.assertEqual(metrics['end_to_end_ms'],20)
            self.assertIsNone(metrics['tool_execution_ms']);self.assertIsNone(metrics['cold_start']);self.assertIsNone(metrics['cost_breakdown'])

    def test_files_allow_immutable_aliases_and_reject_other_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'value').write_text('1');(root/'alias').symlink_to('value')
            self.assertEqual(safe_files(root,{'alias':'value'}),[root/'value'])
            with self.assertRaises(ValueError):safe_files(root)


class EphemeralStateTests(unittest.TestCase):
    def test_faas_state_survives_calls_without_accumulating_local_workspaces(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from atsuite_sdk.state import StateRuntime
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict('os.environ',{'ATSUITE_RUNTIME':'function','PROVIDER':'','ATSUITE_SESSION_ROOT':temporary,'ATSUITE_EPHEMERAL_WORKSPACE':'true'}):
                runtime=StateRuntime();state=runtime.register_object('value',SimpleNamespace(count=0),'unit',session_scoped=True)
                with runtime.execution('unit','session-a','rw','data'):
                    first=runtime.session_dir('session-a');(first/'input.txt').write_text('17');state.count=17
                self.assertFalse(first.exists())
                with runtime.execution('unit','session-a','rw','data'):
                    second=runtime.session_dir('session-a');self.assertNotEqual(first,second)
                    self.assertEqual((second/'input.txt').read_text(),'17');self.assertEqual(state.count,17)
                    state.count+=1
                self.assertFalse(second.exists())
                with runtime.execution('unit','session-a','r','data'):
                    self.assertEqual(state.count,18)
                with runtime.execution('unit','session-b','r','data'):
                    self.assertEqual(state.count,0)


class BudgetTests(unittest.TestCase):
    def test_pools_are_combined_across_provider_families_and_retries_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger=Path(temporary)/'budget.json';config={'budget':{'aws_usd_admission_cap':2,'ali_cny':10,'reserve_fraction':.3}}
            reserve_budget(ledger,config,'aws_lambda','one',.7,'test fixture reservation')
            reserve_budget(ledger,config,'aws_agentcore','two',.7,'test fixture reservation')
            with self.assertRaises(ValueError):reserve_budget(ledger,config,'aws_lambda','three',.01,'test fixture reservation')
            with self.assertRaises(ValueError):reserve_budget(ledger,config,'aws_lambda','one',.01,'test fixture reservation')
            reserve_budget(ledger,config,'ali_fc','four',1,'test fixture reservation')
            self.assertEqual(len(json.loads(ledger.read_text())['reservations']),3)


def report(provider,cost=None):
    return {'provider':provider,'sequence_seed':11,'window_role':'low','request_table_sha256':'paired','planned_requests_sha256':'logical',
        'pilot':False,'requests':53,'success_rate':1,'slo_attainment':1,'total_cost_cny':cost,
        'cost_status':'complete_actual_billing' if cost is not None else 'pending_vendor_billing'}


class FrozenPolicyTests(unittest.TestCase):
    def test_only_original_program_and_parameters_are_admitted(self):
        import hashlib
        from unittest.mock import patch
        from benchmarks.DataSciBench.nodes.AgentTools.frozen_policy import check_analysis,check_preparation
        code='state_out={"value":17}'
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'approved.json'
            path.write_text(json.dumps({'task_id':'fixture','dataset_refs':[{'name':'data.csv','sha256':'fixed','bytes':3}],
                'analyses':[{'step_id':'step','code_sha256':hashlib.sha256(code.encode()).hexdigest(),'outputs':[],'snapshot':0,'seed':0}]}))
            with patch.dict('os.environ',{'WABBER_APPROVED_TRACE':str(path)}):
                check_analysis('fixture','step',code,[],0,0)
                with self.assertRaises(ValueError):check_analysis('fixture','step',code+'\nprint(1)',[],0,0)
                with self.assertRaises(ValueError):check_analysis('fixture','step',code,[],0,1)
                with self.assertRaises(ValueError):check_preparation('fixture',[{'name':'data.csv','sha256':'other','bytes':3}])

    def test_cloud_replay_rejects_state_injection_for_both_lifecycles(self):
        from unittest.mock import patch
        from atsuite_sdk.state import StateRuntime
        with tempfile.TemporaryDirectory() as temporary:
            for family in ('function','mcp'):
                with patch.dict('os.environ',{'ATSUITE_RUNTIME':family,'PROVIDER':'','ATSUITE_SESSION_ROOT':temporary,'ATSUITE_ALLOW_STATE_BOOTSTRAP':'false'}):
                    runtime=StateRuntime()
                    with self.assertRaises(ValueError):
                        with runtime.execution('fixture','uid','rw','domain',{'version':1}):pass


class ResponseTests(unittest.TestCase):
    def test_native_lambda_request_id_wins_over_function_url_gateway_id(self):
        from unittest.mock import MagicMock
        from atsuite.faas.function import FunctionClient
        client=FunctionClient('https://fixture.invalid');response=MagicMock()
        response.headers={'x-amzn-requestid':'gateway-id'}
        response.json.return_value={'status':'ok','result':{'value':17},'metadata':{'native_request_id':'lambda-native-id'},'execution_elapsed_ms':10}
        client.session.post=MagicMock(return_value=response)
        result=client.invoke_result('fixture',{},request_id='client-id')
        self.assertEqual(result.request_id,'client-id');self.assertEqual(result.provider_request_id,'lambda-native-id')


class ComparisonTests(unittest.TestCase):
    def test_no_cost_winner_from_missing_or_incomplete_providers(self):
        self.assertIsNone(compare([report(PROVIDERS[0],1)])['comparisons'][0]['winner'])
        self.assertIsNone(compare([report(p) for p in PROVIDERS])['comparisons'][0]['winner'])

    def test_oracle_does_not_manufacture_a_platform_switch(self):
        reports=[report(p,i+1) for i,p in enumerate(PROVIDERS)]
        result=compare(reports);self.assertEqual(result['comparisons'][0]['winner'],'aws_lambda')
        self.assertFalse(result['observed_provider_switch'])
        reports[0]['slo_attainment']=.5
        self.assertEqual(compare(reports)['comparisons'][0]['winner'],'aws_agentcore')
        reports[1]['planned_requests_sha256']='other'
        with self.assertRaises(ValueError):compare(reports)

    def test_usage_pricing_is_not_an_actual_bill(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'billing.json';path.write_text(json.dumps({'basis':'estimated_from_usage'}))
            with self.assertRaises(ValueError):reconcile([],path)

    def test_duplicate_or_nonconserving_bill_allocations_fail_closed(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);raw=root/'vendor.json';raw.write_text('{"fixture":true}')
            line={'provider':'aws_lambda','record_id':'one','component':'runtime','raw_source':'vendor.json',
                'raw_source_sha256':sha(raw),'amount':'1.20','currency':'USD','allocations':[{'run_key':'a','amount':'1.20','basis':'synthetic unit fixture'}]}
            billing={'basis':'actual_vendor_billing','lines':[line,copy.deepcopy(line)],'coverage':{}}
            path=root/'billing.json';path.write_text(json.dumps(billing))
            with patch('experiments.exp1.analysis.compare_strategies.ROOT',root):
                with self.assertRaises(ValueError):reconcile([{'run_key':'a','provider':'aws_lambda'}],path)
                billing['lines']=[line];billing['lines'][0]['allocations'][0]['amount']='.60';path.write_text(json.dumps(billing))
                with self.assertRaises(ValueError):reconcile([{'run_key':'a','provider':'aws_lambda'}],path)


if __name__=='__main__':unittest.main()


class UsageTests(unittest.TestCase):
    def test_usage_is_session_scoped_deduplicated_and_missing_is_unknown(self):
        from experiments.exp1.runner.provider_usage import session_usage
        a={'eventId':'a','message':json.dumps({'attributes':{'session.id':'s'},'metrics':{'agent.runtime.vcpu.hours.used':.01,'agent.runtime.memory.gb_hours.used':.02}})}
        b={'eventId':'b','message':json.dumps({'attributes':{'session.id':'other'},'metrics':{'agent.runtime.vcpu.hours.used':100}})}
        result=session_usage([a,a,b],'s')
        self.assertEqual(result['log_entries'],1);self.assertEqual(result['vcpu_hours'],.01)
        self.assertIsNone(session_usage([a,b],'absent')['vcpu_hours'])
        missing={'eventId':'c','message':json.dumps({'attributes':{'session.id':'s'},'metrics':{}})}
        self.assertIsNone(session_usage([a,missing],'s')['memory_gb_hours'])


class RenderingEquivalenceTests(unittest.TestCase):
    def test_only_exact_calibrated_png_digest_is_normalized(self):
        from benchmarks.DataSciBench.langgraph_agent.tool_client import ToolClient
        client=object.__new__(ToolClient)
        client.renderings={'curve.png':{'reference_signature':'a'*64,'accepted_signatures':['a'*64,'b'*64]}}
        artifact={'name':'curve.png','signature':'b'*64,'comparison':'decoded-rgba'}
        result=client.comparable({'branches':[{'artifacts':[artifact]}],'seed':11,'code':'unchanged'})
        self.assertEqual(result['branches'][0]['artifacts'][0]['signature'],'a'*64)
        self.assertEqual(artifact['signature'],'b'*64)
        self.assertEqual(result['seed'],11);self.assertEqual(result['code'],'unchanged')
        unknown={**artifact,'signature':'c'*64}
        self.assertEqual(client.comparable(unknown),unknown)
        numeric={**artifact,'comparison':'csv-round-8-decimal-places'}
        self.assertEqual(client.comparable(numeric),numeric)


class ReservationRevisionTests(unittest.TestCase):
    def test_usage_projection_does_not_become_a_bill(self):
        from experiments.exp1.runner.static_deployment_runner import reassess_reservation
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);ledger=root/'ledger.json';evidence=root/'usage.json';evidence.write_text('{"scope":"synthetic contract fixture"}')
            ledger.write_text(json.dumps({'reservations':[{'run_key':'r','reserved_amount':'1.0','basis':'original cap','actual_charge':None}]}))
            proof=[{'path':str(evidence),'sha256':sha(evidence)}]
            row=reassess_reservation(ledger,'r','.1','Synthetic usage projection fixture, not a bill',proof)
            self.assertIsNone(row['actual_charge']);self.assertEqual(row['reserved_amount'],'0.1');self.assertEqual(row['revisions'][0]['previous_amount'],'1.0')
            with self.assertRaises(ValueError):reassess_reservation(ledger,'r','.2','increase',proof)
            evidence.write_text('changed')
            with self.assertRaises(ValueError):reassess_reservation(ledger,'r','.05','changed evidence',proof)


class ReplayRenderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_normalizes_only_the_calibrated_artifact_digest(self):
        from unittest.mock import Mock
        from benchmarks.DataSciBench.langgraph_agent.trace_replayer import ReplayModel
        from benchmarks.DataSciBench.langgraph_agent.tool_client import ToolClient
        client=object.__new__(ToolClient);client.renderings={'p.png':{'reference_signature':'a'*64,'accepted_signatures':['a'*64,'b'*64]}}
        source={'artifacts':[{'name':'p.png','comparison':'decoded-rgba','signature':'a'*64}],'snapshot':2}
        actual=copy.deepcopy(source);actual['artifacts'][0]['signature']='b'*64
        model=object.__new__(ReplayModel);model.normalizer=client.comparable;model.recorder=Mock();model.index=0
        model.llm=[{'phase':'REVIEW','iteration':0,'request':{'messages':[{'role':'user','content':json.dumps(source)}]},'duration_s':0,'decision':{'status':'complete'}}]
        self.assertEqual(await model.decide('REVIEW',0,[{'role':'user','content':json.dumps(actual)}]),{'status':'complete'})
        model.index=0;actual['snapshot']=3
        with self.assertRaises(ValueError):await model.decide('REVIEW',0,[{'role':'user','content':json.dumps(actual)}])
