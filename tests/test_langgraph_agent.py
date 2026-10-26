"""Unit doubles exercise control flow; real acceptance uses DeepSeek and Docker RPC."""
import asyncio,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,MagicMock,AsyncMock
from benchmarks.DataSciBench.langgraph_agent.state import merge_results
from benchmarks.DataSciBench.langgraph_agent.validators import relative_file,validate_plan
from benchmarks.DataSciBench.langgraph_agent.graph import build_graph
from benchmarks.DataSciBench.langgraph_agent.tool_client import ToolClient,DivergenceError
from benchmarks.DataSciBench.langgraph_agent.trace_recorder import TraceRecorder,export_atg
from benchmarks.DataSciBench.langgraph_agent.trace_replayer import ReplayModel
from atsuite_sdk.response import ToolResponse
from atsuite_sdk.workflow import Trace


def step(name,deps):
    return {'id':name,'deps':deps,'purpose':name,'prompt_evidence':'analyze',
            'outputs':[],'code':f'state_out = {{"{name}": 1}}'}


class StateAndPlanTests(unittest.TestCase):
    def test_reducer_rejects_conflicting_writes(self):
        self.assertEqual(merge_results({'a':1},{'b':2}),{'a':1,'b':2})
        with self.assertRaises(ValueError):merge_results({'a':1},{'a':2})

    def test_rejects_cycles_duplicate_computation_and_output_conflicts(self):
        for steps in ([step('a',['b']),step('b',['a'])],[step('a',[]),{**step('b',[]),'code':step('a',[])['code']}],
                      [{**step('a',[]),'outputs':['same.csv']},{**step('b',[]),'outputs':['same.csv']} ]):
            with self.subTest(steps=steps):
                with self.assertRaises(ValueError):validate_plan({'steps':steps},'analyze')

    def test_evidence_handles_upstream_duplicate_quotes(self):
        s=step('a',[]);s['prompt_evidence']='a "chart.png"'
        self.assertEqual(validate_plan({'steps':[s]},'a ""chart.png""'),[s])
        s['prompt_evidence']='invented computation'
        with self.assertRaises(ValueError):validate_plan({'steps':[s]},'a "chart.png"')

    def test_rejects_file_escape_and_reserved_runtime_outputs(self):
        for name in ('../secret','/etc/passwd','.state/key','a\\b','.'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):relative_file(name)
        with self.assertRaises(ValueError):validate_plan({'steps':[{**step('a',[]),'outputs':['state.pkl']}]},'analyze')


class GraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stategraph_send_fanout_and_barrier_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder=TraceRecorder(Path(directory)/'trace','request','session')
            class Model:
                async def decide(self,phase,iteration,messages):
                    return {'steps':[step('prep',[]),step('a',['prep']),step('b',['prep'])]} if phase=='PLAN' else {'verdict':'complete','feedback':'unit stub'}
            class Tools:
                def __init__(self):self.arrived=set();self.barrier=asyncio.Event();self.batches=[]
                async def call(self,name,args,key,wave=None):
                    if name=='prepare_workspace':return {'status':'ok','snapshot':0}
                    if name=='execute_analysis':
                        sid=args['step_id']
                        if sid in ('a','b'):
                            self.arrived.add(sid)
                            if len(self.arrived)==2:self.barrier.set()
                            await self.barrier.wait()
                        return {'status':'ok','step_id':sid,'branch_ref':key,'artifacts':[]}
                    if name=='merge_artifacts':
                        self.batches.append([b['step_id'] for b in args['branches']])
                        return {'status':'ok','snapshot':args['snapshot']+1,'artifacts':[]}
                    return {'status':'ok','artifacts':[]}
            tools=Tools();graph=build_graph(Model(),tools,recorder,[])
            result=await asyncio.wait_for(graph.ainvoke({'request_id':'r','session_id':'s','task_id':'unit',
                'user_prompt':'analyze','messages':[],'dataset_refs':[],'iteration':0,'completed':[],
                'tool_results':{},'artifacts':[],'snapshot':0,'workspace_id':''}),5)
            self.assertEqual(result['status'],'complete')
            self.assertEqual(tools.batches,[['prep'],['a','b']])
            self.assertEqual(len(result['tool_results']),3)

    async def test_replay_fails_before_incompatible_downstream_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);capture=root/'capture';capture.mkdir()
            event={'kind':'tool','key':'call','tool_name':'execute_analysis','args':{'code':'x'},'result':{'status':'ok','state_signature':'expected'}}
            (capture/'events.jsonl').write_text(json.dumps(event)+'\n')
            adapter=MagicMock();adapter.targets={'t':object()};adapter.invoke.return_value=ToolResponse(result={'status':'ok','state_signature':'changed'})
            recorder=TraceRecorder(root/'replay','r','s')
            with patch('benchmarks.DataSciBench.langgraph_agent.tool_client.create_runtime_adapter',return_value=adapter):
                client=ToolClient({'provider':'unit','family':'faas','target_id':'t','endpoint':'local'},recorder,capture)
                with self.assertRaises(DivergenceError):await client.call('execute_analysis',{'code':'different'},'call')
                adapter.invoke.assert_not_called()
                with self.assertRaises(DivergenceError):await client.call('execute_analysis',{'code':'x'},'call')
                self.assertEqual(client.consumed,set())

    async def test_replay_uses_recorded_decision_and_duration_without_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);capture=root/'capture';capture.mkdir()
            event={'kind':'llm','phase':'PLAN','iteration':0,'duration_s':1.25,'request':{'messages':[]},'decision':{'steps':['captured']}}
            (capture/'events.jsonl').write_text(json.dumps(event)+'\n');(capture/'result.json').write_text('{"status":"complete"}')
            replay=ReplayModel(capture,TraceRecorder(root/'replay','r','s'))
            with patch('asyncio.sleep',new_callable=AsyncMock) as sleep:
                self.assertEqual(await replay.decide('PLAN',0,[]),event['decision']);sleep.assert_awaited_once_with(1.25)
            replay.assert_consumed()


class ATGTests(unittest.TestCase):
    def test_atg_preserves_parallel_edges(self):
        events=[{'kind':'llm','phase':'PLAN','duration_s':1,'response':{}},
                {'kind':'wave_start'},
                {'kind':'tool','tool_name':'a','wave':'one','duration_s':2,'args':{}},
                {'kind':'tool','tool_name':'b','wave':'one','duration_s':3,'args':{}},
                {'kind':'tool','tool_name':'merge','duration_s':.1,'args':{}}]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'atg.json';export_atg(events,path);trace=Trace.from_file(path)
            self.assertEqual([e.target_id for e in trace.nodes[1].edge_to],[2,3])
            self.assertEqual([e.target_id for e in trace.nodes[2].edge_to],[4])
            self.assertEqual([e.target_id for e in trace.nodes[3].edge_to],[4])


class ModelRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_retry_is_recorded_without_hidden_sdk_retries(self):
        import os,httpx
        from types import SimpleNamespace
        from openai import RateLimitError
        from benchmarks.DataSciBench.langgraph_agent.model_client import DeepSeekClient
        request=httpx.Request('POST','https://api.deepseek.com/chat/completions')
        error=RateLimitError('rate limited',response=httpx.Response(429,request=request),body={'error':'rate limited'})
        raw={'id':'unit-response','model':'deepseek-flash','usage':{'prompt_tokens':3,'completion_tokens':2}}
        response=SimpleNamespace(model_dump=lambda **kw:raw,choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='{}'))],model='deepseek-flash',id='unit-response')
        client=MagicMock();client.api_key='unit-key'
        client.chat.completions.create=AsyncMock(side_effect=[error,response])
        with tempfile.TemporaryDirectory() as directory:
            recorder=TraceRecorder(Path(directory)/'trace','r','s')
            config={'seed':0,'temperature':0,'top_p':1,'max_tokens':1000,'retries':1}
            with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'unit-key'}),patch('benchmarks.DataSciBench.langgraph_agent.model_client.AsyncOpenAI',return_value=client) as factory,patch('asyncio.sleep',new_callable=AsyncMock) as sleep:
                model=DeepSeekClient(recorder,config);self.assertEqual(await model.decide('PLAN',0,[]),{})
                self.assertEqual(factory.call_args.kwargs['max_retries'],0)
                self.assertEqual(client.chat.completions.create.await_count,2);sleep.assert_awaited_once()
            self.assertEqual(recorder.events[0]['status_code'],429)
            self.assertEqual(recorder.events[-1]['input_tokens'],3)
            self.assertEqual(recorder.events[-1]['output_tokens'],2)
            self.assertEqual(recorder.events[-1]['attempts'],2)


if __name__=='__main__':unittest.main()
