"""Run offline, resource-limited Docker profiles; preserve failures and publish evidence."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import time
import uuid

ROOT=Path(__file__).resolve().parents[3]
TASKS=['human_15','csv_excel_34','human_142','human_12','human_7','csv_excel_29']


def sha(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def image_info(image):
    return json.loads(subprocess.check_output(['docker','image','inspect',image]))[0]


def run_profiles(image,tasks,repeats=3,seed=0):
    from experiments.exp1.data.prepare_datasets import LOCK,DATA,checked
    lock=json.loads(LOCK.read_text())
    for task in tasks:
        record=lock['tasks'].get(task)
        if not record or record['status']!='prepared':raise ValueError(f'Unprepared task: {task}')
        for item in record['files']:checked(DATA/task/item['name'],item)
    image_id=image_info(image)['Id']
    batch_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:12]
    directory=ROOT/'results/phase1'/batch_id;directory.mkdir(parents=True)
    packages=subprocess.check_output(['docker','run','--rm','--network','none','--entrypoint','python',image_id,'-m','pip','freeze'],text=True)
    (directory/'packages.txt').write_text(packages)
    (directory/'docker-inspect.json').write_text(json.dumps(image_info(image_id),indent=2))
    cpu_model=next((line.split(':',1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines() if line.startswith('model name')),None)
    environment={'image_id':image_id,'base_image_id':image_info('uaib-mcp-datascibench-datasci:latest')['Id'],
       'cpu_model':cpu_model,'cpu_limit':2,'memory_limit_bytes':4*1024**3,'threads':1,'network':'none',
       'seed':seed,'repetitions':repeats,'packages':packages.splitlines(),
       'code_sha256':{str(p.relative_to(ROOT)):sha(p) for p in [*sorted((ROOT/'experiments/exp1/profile').glob('*.py')),
                 ROOT/'experiments/exp1/profile/Dockerfile',ROOT/'benchmarks/DataSciBench/nodes/DataSci/implementation.py',ROOT/'benchmarks/DataSciBench/nodes/DataSci/worker.py',ROOT/'atsuite_sdk/state.py',ROOT/'atsuite_sdk/abstract.py']}}
    runs=[]
    for task in tasks:
        for repeat in range(repeats):
            if any(sha(ROOT/path)!=expected for path,expected in environment['code_sha256'].items()):
                raise ValueError('Reference code changed during profiling; start a new batch')
            run_id=uuid.uuid4().hex;out=directory/f'{task}-{repeat}-{run_id}';out.mkdir()
            name='wabber-phase1-'+run_id
            command=['docker','run','--name',name,'--network','none','--cpus','2','--memory','4g','--memory-swap','4g',
               '--pids-limit','256','--read-only','--user',f'{os.getuid()}:{os.getgid()}',
               '--tmpfs','/tmp:rw,nosuid,size=512m','-v',f'{ROOT}:/repo:ro','-v',f'{DATA}:/inputs:ro',
               '-v',f'{out}:/out:rw',image_id,task,run_id,str(seed)]
            print(f'{task} repeat {repeat+1}/{repeats} {run_id}',flush=True)
            event={'run_id':run_id,'request_id':run_id+'-tool','task_id':task,'repeat':repeat,
                'utc':datetime.now(timezone.utc).isoformat(),'command':command,'directory':str(out.relative_to(ROOT))}
            start=time.monotonic()
            try:
                with (out/'container.log').open('w') as log:
                    result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=720)
                event.update(exit_code=result.returncode,status='ok' if result.returncode==0 else 'failed')
            except subprocess.TimeoutExpired:
                subprocess.run(['docker','kill',name],capture_output=True)
                event.update(exit_code=None,status='timeout')
            finally:
                event['container_monotonic_seconds']=time.monotonic()-start
                inspected=subprocess.run(['docker','inspect',name],capture_output=True,text=True)
                event['container_state']=json.loads(inspected.stdout)[0]['State'] if inspected.returncode==0 else None
                subprocess.run(['docker','rm','-f',name],capture_output=True)
            if (out/'profile.json').exists():event['profile']=json.loads((out/'profile.json').read_text())
            else:event['profile']=None
            (out/'execution.json').write_text(json.dumps(event,indent=2)+'\n')
            runs.append(event)
            (directory/'batch.json').write_text(json.dumps({'batch_id':batch_id,'environment':environment,'runs':runs},indent=2)+'\n')
            print(task,event['status'],event['profile']['compute_wall_seconds'] if event['profile'] else None,flush=True)
    print(directory,flush=True)
    return directory


def publish(directory):
    from experiments.exp1.data.import_prompts import write_manifest
    batch=json.loads((directory/'batch.json').read_text());env=batch['environment'];runs=batch['runs']
    if env['repetitions']<3:raise ValueError('Final selection requires three independent Docker runs per task')
    source=json.loads((ROOT/'benchmarks/DataSciBench/tasks/catalog.json').read_text())
    lock=json.loads((ROOT/'benchmarks/DataSciBench/tasks/datasets.lock.json').read_text())
    if any(sha(ROOT/path)!=expected for path,expected in env.get('code_sha256',{}).items()):
        raise ValueError('Reference code changed since measurement')
    selected=[]
    for task in dict.fromkeys(r['task_id'] for r in runs):
        group=[r for r in runs if r['task_id']==task]
        if len(group)!=env['repetitions'] or any(r['status']!='ok' or not r['profile'] or r['profile']['status']!='ok' for r in group):
            raise ValueError(f'Cannot publish unsuccessful task {task}')
        record=next(t.copy() for t in source['tasks'] if t['task_id']==task)
        profiles=[r['profile'] for r in group]
        hashes=[{p['name']:p['sha256'] for p in profile['outputs']} for profile in profiles]
        if any(h!=hashes[0] for h in hashes[1:]):
            raise ValueError(f'Output hashes differ across seeded repeats: {task}')
        median=statistics.median(p['compute_wall_seconds'] for p in profiles)
        record.update(readiness={'status':'offline_reference_validated','metadata_review':'Original prompt and required files manually reconciled; static extraction warnings remain as diagnostics','execution':'existing ATSuite datasci_run_code in Docker; no LLM trajectory',
            'inputs':lock['tasks'][task]},
            task_classification={'compute_class':'light' if median<5 else 'medium' if median<15 else 'heavy',
                'basis':'median measured compute wall seconds: light <5, medium 5–15, heavy >=15; no synthetic padding',
                'memory_class':'high' if max(p['process_peak_rss_bytes'] for p in profiles)>=512*1024**2 else 'ordinary'},
            profiling={'compute_wall_seconds_median':median,'compute_wall_seconds_runs':[p['compute_wall_seconds'] for p in profiles],
               'cpu_seconds_median':statistics.median(p['cpu_seconds'] for p in profiles),
               'peak_rss_bytes_max':max(p['process_peak_rss_bytes'] for p in profiles),
               'shared_state_bytes_max':max(p['max_shared_state_bytes'] for p in profiles),
               'workspace_bytes_max':max(p['workspace_bytes'] for p in profiles),
               'max_step_wall_seconds_median':statistics.median(max(s['wall_seconds'] for s in p['steps']) for p in profiles),
               'state_encoding':profiles[0]['shared_state_encoding'], 'parameters':profiles[0]['parameters'],
               'subtask_dag':profiles[0]['dag'],'parallelism':'DAG identifies potential independent consumers of prepared immutable inputs; measurements execute serially',
               'validation_checks':profiles[0]['validation'],'all_output_hashes_reproduce':True,
               'output_sha256':hashes[0],
               'evidence':[r['directory'] for r in group]})
        # Output filenames remain the original prompt contract, independent of auxiliary diagnostic outputs.
        record['readiness']['expected_outputs']=record['output_files']
        selected.append(record)
    if not 4<=len(selected)<=8:raise ValueError('Expected 4–8 representative tasks')
    manifest={**source,'task_count':len(selected),'tasks':selected,'environment':env,
       'selection':{'source_task_count':source['task_count'],
          'upstream_task_count':source.get('selection',{}).get('upstream_task_count',source['task_count']),
          'catalog_excluded_task_count':source.get('selection',{}).get('excluded_task_count',0),
          'restored_verified_tasks':source.get('selection',{}).get('restored_verified_tasks',[]),'pending_task_count':source['task_count']-len(selected)-sum(v['status']=='blocked' for v in lock['tasks'].values()),
          'rule':'Use the screened catalog; only measured successful offline reference tasks enter the active manifest.',
          'excluded':[{'task_id':k,**v} for k,v in lock['tasks'].items() if v['status']=='blocked'],
          'batch_evidence':str(directory.relative_to(ROOT))}}
    write_manifest(manifest,ROOT/'benchmarks/DataSciBench/tasks/manifest.json')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--image',default='wabber-phase1-reference:20261025-cpu-lstm')
    p.add_argument('--task',action='append');p.add_argument('--repeats',type=int,default=3);p.add_argument('--seed',type=int,default=0)
    p.add_argument('--publish',type=Path)
    a=p.parse_args()
    if a.publish:publish(a.publish)
    else:run_profiles(a.image,a.task or TASKS,a.repeats,a.seed)
