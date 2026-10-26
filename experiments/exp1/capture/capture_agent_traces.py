"""Live LangGraph Capture and decision Replay, using real ATSuite Tool RPC."""
import argparse,asyncio,json,traceback,uuid
from pathlib import Path
from dotenv import load_dotenv
from benchmarks.DataSciBench.langgraph_agent.graph import build_graph
from benchmarks.DataSciBench.langgraph_agent.model_client import DeepSeekClient
from benchmarks.DataSciBench.langgraph_agent.tool_client import ToolClient
from benchmarks.DataSciBench.langgraph_agent.trace_recorder import TraceRecorder
from benchmarks.DataSciBench.langgraph_agent.trace_replayer import ReplayModel

ROOT=Path(__file__).resolve().parents[3]


async def run(task_id,config,output,capture=None):
    tasks=json.loads((ROOT/'benchmarks/DataSciBench/tasks/manifest.json').read_text())
    task=next(t for t in tasks['tasks'] if t['task_id']==task_id)
    session_id=uuid.uuid4().hex;request_id=uuid.uuid4().hex
    recorder=TraceRecorder(output,request_id,session_id)
    initial={'request_id':request_id,'session_id':session_id,'task_id':task_id,'user_prompt':task['prompt'],
         'messages':[],'workspace_id':'','dataset_refs':[{k:f[k] for k in ('name','sha256','bytes')} for f in task['readiness']['inputs']['files']],
         'execution_plan':[],'tool_results':{},'artifacts':[],'iteration':0,'status':'new','error':None,'completed':[],'snapshot':0}
    recorder.emit('run_start',mode='replay' if capture else 'capture',task_id=task_id,prompt_sha256=task['prompt_sha256'],
         config=config,source_capture=str(capture) if capture else None,execution_scope=config['tool']['execution_scope'])
    tools=model=None;state=initial
    try:
        tools=ToolClient(config['tool'],recorder,capture)
        model=ReplayModel(capture,recorder) if capture else DeepSeekClient(recorder,config['model'])
        graph=build_graph(model,tools,recorder,task['output_files'],config['max_iterations'],config['model']['seed'])
        async for current in graph.astream(initial,config={'recursion_limit':150,'max_concurrency':config['max_parallel']},stream_mode='values'):
            state=current
        if state['status']!='complete':raise RuntimeError('Agent did not complete: '+state.get('feedback',''))
        if capture:model.assert_consumed();tools.assert_consumed()
        recorder.finish(state)
        return {'status':'complete','directory':str(output),'task_id':task_id,'iterations':state['iteration']+1}
    except BaseException as exc:
        recorder.emit('run_error',error_type=type(exc).__name__,error=str(exc),traceback=traceback.format_exc())
        recorder.finish({**state,'status':'failed','error':{'type':type(exc).__name__,'message':str(exc)}})
        raise
    finally:
        if tools:tools.close()
        if model and not capture:await model.client.close()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--task',required=True)
    p.add_argument('--config',type=Path,default=ROOT/'benchmarks/DataSciBench/config/langgraph_experiment.json')
    p.add_argument('--output',type=Path);p.add_argument('--replay',type=Path)
    a=p.parse_args();load_dotenv(ROOT/'.env',override=False)
    config=json.loads(a.config.read_text());output=a.output or ROOT/'results/phase2'/uuid.uuid4().hex
    print(json.dumps(asyncio.run(run(a.task,config,output,a.replay)),ensure_ascii=False))


if __name__=='__main__':main()
