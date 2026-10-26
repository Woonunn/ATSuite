"""Real local RPC + Docker lifecycle regression; synthetic values are test fixtures."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import threading
import uuid
from atsuite.runtime import InvocationRequest,create_runtime_adapter

ROOT=Path(__file__).resolve().parents[3]


@contextmanager
def service(family,area,image):
    area.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env={k:v for k,v in os.environ.items() if not any(s in k for s in ('TOKEN','SECRET','CREDENTIAL','API_KEY','ACCESS_KEY','DEEPSEEK','OPENAI'))}
    env.update(PROVIDER='',ATSUITE_RUNTIME='mcp' if family=='mcp' else 'function',
        ATSUITE_STATE_SERVICE='datasci-agent',ATSUITE_SESSION_ROOT=str(area/'state'),ATSUITE_EVENT_LOG_DIR=str(area/'events'),
        WABBER_DATA_ROOT=str(ROOT/'benchmarks/DataSciBench/data'),WABBER_SANDBOX_IMAGE=image,WABBER_CODE_TIMEOUT='180')
    with (area/'service.log').open('w') as log:
        process=subprocess.Popen([sys.executable,'-m','experiments.exp1.capture.local_tool_service','--family',family,'--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log,start_new_session=True)
        try:
            started=time.perf_counter()
            while True:
                if process.poll() is not None:raise RuntimeError((area/'service.log').read_text())
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=.2):break
                except OSError:
                    if time.perf_counter()-started>30:raise TimeoutError('Tool frontend startup')
                    time.sleep(.05)
            yield {'provider':'local','family':'session' if family=='mcp' else 'faas','target_id':'datasci-agent','endpoint':f'http://127.0.0.1:{port}',
                   'timeout':240,'execution_scope':'wlh-190 Docker lifecycle regression; not cloud billing'}
        finally:
            os.killpg(process.pid,signal.SIGTERM)
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()


def fixture(family,index,area,image,barrier=None):
    with service(family,area,image) as config:
        adapter=create_runtime_adapter('local',config['family'])
        adapter.connect({'targets':{'datasci-agent':{'endpoint':config['endpoint'],'family':config['family']}}})
        uid=uuid.uuid4().hex;session=adapter.open_session(adapter.targets['datasci-agent'],uid);calls=[]
        def call(name,args,expected='ok'):
            response=adapter.invoke(InvocationRequest(target_id='datasci-agent',tool_name=name,args=args,uid=uid,
                call_id=uuid.uuid4().hex,session=session,timeout=240,run_id=uid))
            calls.append({'tool':name,'response':response.to_dict()})
            result=response.result
            assert isinstance(result,dict) and result['status']==expected,(response.error,result)
            if expected=='ok':assert response.status=='ok',response.error
            else:assert response.status!='ok','Tool failure was hidden'
            return result
        try:
            manifest=json.loads((ROOT/'benchmarks/DataSciBench/tasks/manifest.json').read_text())
            task=next(t for t in manifest['tasks'] if t['task_id']=='human_15')
            refs=[{k:r[k] for k in ('name','bytes','sha256')} for r in task['readiness']['inputs']['files']]
            prepared=call('prepare_workspace',{'task_id':'human_15','dataset_refs':refs})
            if barrier:barrier.wait(timeout=90)
            def analyze(name,code,generation=1,outputs=None):
                return call('execute_analysis',{'step_id':name,'code':code,'outputs':outputs or [],'snapshot':generation,'seed':index,'call_id':name})
            produced=analyze('producer',f"import numpy as np\nstate_out={{'data':np.full(10000,{index},dtype='int64')}}\n(output_dir/'session.txt').write_text('{index}')",0,['session.txt'])
            branch={'step_id':'producer','branch_ref':produced['branch_ref'],'artifacts':[{k:a[k] for k in ('name','signature','comparison')} for a in produced['artifacts']]}
            merged=call('merge_artifacts',{'branches':[branch],'snapshot':0})
            snapshot=area/'state/.snapshots/datasci-agent'/(uid+'.json')
            saved=snapshot.read_bytes() if snapshot.exists() else None
            code=f"import numpy as np\nfrom pathlib import Path\nassert Path(artifact_path('session.txt')).read_text()=='{index}'\nassert (shared['producer']['data']=={index}).all()\nstate_out={{'sum':int(shared['producer']['data'].sum())}}"
            # Same Session, concurrent readers, distinct output/state namespaces.
            with ThreadPoolExecutor(max_workers=2) as pool:
                a,b=list(pool.map(lambda pair:analyze(*pair),[('read_a',code),('read_b',code)]))
            assert min(a['exec_ended_monotonic'],b['exec_ended_monotonic'])>max(a['exec_started_monotonic'],b['exec_started_monotonic']),'Safe reads were serialized'
            analyze('mutate',"shared['producer']['data'][:]=-999\nstate_out={}")
            analyze('after_mutation',code)
            call('execute_analysis',{'step_id':'bad','code':'1/0','outputs':[],'snapshot':1,'seed':index,'call_id':'bad'},expected='error')
            analyze('after_failure',code)
            if family=='mcp':
                names={r['response']['result']['container_name'] for r in calls if r['tool']=='execute_analysis'}
                assert names=={prepared['kernel']['container_name']},names
                assert not snapshot.exists(),'MCP unexpectedly committed an SDK snapshot'
                assert not any((area/'state/datasci-agent'/uid/'workspace').glob('v*/state.pkl')),'MCP canonical state was checkpointed'
                for r in calls:
                    result=r['response']['result'];metrics=result.get('state_lifecycle') or result.get('kernel_execution',{}).get('state_lifecycle')
                    if metrics:assert not metrics['canonical_restore_performed'] and not metrics['canonical_save_performed']
            else:
                assert saved is not None and saved==snapshot.read_bytes(),'Read-only calls committed canonical state'
                names=[r['response']['result']['container_name'] for r in calls if r['tool']=='execute_analysis']
                assert len(names)==len(set(names)),'FaaS reused a resident computation container'
                assert a['state_lifecycle']['canonical_restore_performed'] and a['state_lifecycle']['canonical_restore_bytes']>0
                assert merged['kernel_execution']['state_lifecycle']['canonical_save_performed']
            report={'family':family,'index':index,'uid':uid,'isolation':'dedicated RPC process + dedicated container per MCP Session; FaaS per-call container',
                    'calls':calls,'status':'passed'}
            (area/'validation.json').write_text(json.dumps(report,indent=2)+'\n')
            return {'family':family,'index':index,'uid':uid,'status':'passed','directory':str(area),'containers':sorted(names)}
        finally:adapter.close_session(session)


def main():
    p=argparse.ArgumentParser();p.add_argument('--sessions',type=int,default=16);p.add_argument('--image',default=os.getenv('WABBER_SANDBOX_IMAGE'));a=p.parse_args()
    if not a.image or not a.image.startswith('sha256:'):p.error('Supply a pinned --image')
    area=ROOT/'results/phase2'/('lifecycle-'+uuid.uuid4().hex);area.mkdir(parents=True)
    barrier=threading.Barrier(a.sessions)
    with ThreadPoolExecutor(max_workers=a.sessions) as pool:
        sessions=list(pool.map(lambda i:fixture('mcp',i,area/f'mcp-{i}',a.image,barrier),range(a.sessions)))
    sessions.append(fixture('faas',101,area/'faas',a.image))
    result={'scope':'Real isolated local execution environments; synthetic state fixtures, not production performance/billing','image':a.image,'sessions':sessions}
    (area/'validation.json').write_text(json.dumps(result,indent=2)+'\n');print(area)


if __name__=='__main__':main()
