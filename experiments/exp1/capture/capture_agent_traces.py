"""Live LangGraph Capture and decision Replay, using real ATSuite Tool RPC."""
import argparse,asyncio,json,traceback,uuid,time
from pathlib import Path
from atsuite.tracing import TraceCollector
from atsuite.tracing.langgraph import LangGraphTracingAdapter
from dotenv import load_dotenv
from benchmarks.DataSciBench.langgraph_agent.graph import build_graph
from benchmarks.DataSciBench.langgraph_agent.model_client import DeepSeekClient
from benchmarks.DataSciBench.langgraph_agent.tool_client import ToolClient
from benchmarks.DataSciBench.langgraph_agent.trace_recorder import TraceRecorder
from benchmarks.DataSciBench.langgraph_agent.trace_replayer import ReplayModel

ROOT=Path(__file__).resolve().parents[3]


async def run(task_id,config,output,capture=None,workload_model=None):
    tasks=json.loads((ROOT/'benchmarks/DataSciBench/tasks/manifest.json').read_text())
    task=next(t for t in tasks['tasks'] if t['task_id']==task_id)
    session_id=uuid.uuid4().hex;request_id=uuid.uuid4().hex
    tracing=LangGraphTracingAdapter(TraceCollector(request_id,session_id,output))
    recorder=TraceRecorder(output,request_id,session_id,tracing)
    initial={'request_id':request_id,'session_id':session_id,'task_id':task_id,'user_prompt':task['prompt'],
         'messages':[],'workspace_id':'','dataset_refs':[{k:f[k] for k in ('name','sha256','bytes')} for f in task['readiness']['inputs']['files']],
         'execution_plan':[],'tool_results':{},'artifacts':[],'iteration':0,'status':'new','error':None,'completed':[],'snapshot':0}
    arrival_ns=time.time_ns()
    if workload_model is not None:
        from atsuite.tracing.storage import write_json
        prediction=workload_model.begin(request_id,session_id,arrival_ns)
        write_json(output/'workload_prediction.json',prediction)
    recorder.emit('run_start',mode='replay' if capture else 'capture',task_id=task_id,prompt_sha256=task['prompt_sha256'],
         config=config,source_capture=str(capture) if capture else None,execution_scope=config['tool']['execution_scope'])
    tools=model=None;state=initial
    try:
        tools=await asyncio.to_thread(ToolClient,config['tool'],recorder,capture)
        model=ReplayModel(capture,recorder,tools.comparable if tools.renderings else None) if capture else DeepSeekClient(recorder,config['model'])
        graph=build_graph(model,tools,recorder,task['output_files'],config['max_iterations'],config['model']['seed'])
        async for current in tracing.astream(graph,initial,config={'recursion_limit':150,'max_concurrency':config['max_parallel']}):
            state=current
        if state['status']!='complete':raise RuntimeError('Agent did not complete: '+state.get('feedback',''))
        if capture:model.assert_consumed();tools.assert_consumed()
        return {'status':'complete','directory':str(output),'task_id':task_id,'iterations':state['iteration']+1}
    except BaseException as exc:
        recorder.emit('run_error',error_type=type(exc).__name__,error=str(exc),traceback=traceback.format_exc())
        state={**state,'status':'failed','error':{'type':type(exc).__name__,'message':str(exc)}}
        raise
    finally:
        try:
            if tools:await tools.aclose()
        finally:
            recorder.finish(state)
            if workload_model is not None:
                from atsuite.tracing.storage import write_json
                trace=tracing.collector.snapshot()
                trace.update(arrival_ns=arrival_ns,completed_ns=time.time_ns(),
                             request_status='success' if state['status']=='complete' else 'failed')
                profile=workload_model.observe(trace)
                write_json(output/'workload_profile.json',profile)
            if model and not capture:await model.client.close()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--task',required=True)
    p.add_argument('--config',type=Path,default=ROOT/'benchmarks/DataSciBench/config/langgraph_experiment.json')
    p.add_argument('--output',type=Path);p.add_argument('--replay',type=Path)
    p.add_argument('--workload-model',type=Path,help='Load/update an empirical workload snapshot')
    a=p.parse_args();load_dotenv(ROOT/'.env',override=False)
    config=json.loads(a.config.read_text());output=a.output or ROOT/'results/phase2'/uuid.uuid4().hex
    from atsuite.workload import WorkloadModel
    from atsuite.tracing.storage import trace_path
    model=(WorkloadModel.load(a.workload_model) if trace_path(a.workload_model).exists() else WorkloadModel()) if a.workload_model else None
    try:
        print(json.dumps(asyncio.run(run(a.task,config,output,a.replay,model)),ensure_ascii=False))
    finally:
        if model is not None:model.save(a.workload_model)


if __name__=='__main__':main()
