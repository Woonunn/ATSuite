#!/bin/bash
set -euo pipefail
python3 - "$1" <<'PY'
import json,os,shutil,sys,hashlib,yaml
from pathlib import Path
out=Path(sys.argv[1])
# The builder copies this script; the source data directory remains on the host.
repo=Path.cwd()
while not (repo/'benchmarks/DataSciBench/tasks/manifest.json').exists():
    if repo==repo.parent:raise RuntimeError('Run ATSuite build from the Wabber repository')
    repo=repo.parent
source=repo/'benchmarks/DataSciBench'
shutil.copy2(source/'nodes/DataSci/kernel.py',out/'kernel.py')
shutil.copy2(source/'tasks/evaluation_alignment.json',out/'evaluation_alignment.json')
data=out/'datasets';data.mkdir(exist_ok=True)
workload=os.environ.get('WABBER_BUILD_WORKLOAD')
if workload:
    sys.path.insert(0,str(repo))
    from experiments.exp1.data.prepare_e1b import load,sha
    sealed,_=load(workload)
    task_ids=list(sealed['tasks'])
else:
    task_ids=[yaml.safe_load((repo/'experiments/exp1/configs/e1a.yaml').read_text())['task_id']]
manifest=json.loads((source/'trace/captured/manifest.json').read_text())
policies={}
for task_id in task_ids:
    shutil.copytree(source/'data'/task_id,data/task_id,dirs_exist_ok=True)
    record=next(t for t in manifest['captures'] if t['task_id']==task_id)
    capture=repo/record['capture']
    raw=(capture/'events.jsonl').read_bytes()
    assert hashlib.sha256(raw).hexdigest()==record['files']['events.jsonl'],'Capture changed before build'
    events=[json.loads(line) for line in raw.splitlines()]
    prepared=next(e for e in events if e['kind']=='tool' and e['tool_name']=='prepare_workspace')
    approved={'task_id':task_id,'capture_events_sha256':hashlib.sha256(raw).hexdigest(),'dataset_refs':prepared['args']['dataset_refs'],'analyses':[],
              'scope':'closed-program replay only; sealed captured code and parameters; no new generated code or state bootstrap'}
    for event in events:
        if event['kind']=='tool' and event['tool_name']=='execute_analysis':
            args=event['args'];approved['analyses'].append({k:args[k] for k in ('step_id','outputs','snapshot','seed')}|{'code_sha256':hashlib.sha256(args['code'].encode()).hexdigest()})
    policies[task_id]=approved
if workload:
    approved={'tasks':policies,'workload_manifest_sha256':sha(Path(workload)/'manifest.json')}
    with (out/'requirements.txt').open('a') as f:
        f.write('\n--extra-index-url https://download.pytorch.org/whl/cpu\ntorch==2.5.1\nopenpyxl==3.1.5\nxlrd==2.0.1\nmlxtend==0.22.0\n')
(out/'approved_trace.json').write_text(json.dumps(approved,indent=2)+'\n')
settings={'ATSUITE_ALLOW_STATE_BOOTSTRAP':'false','WABBER_NATIVE_CONFINEMENT':'frozen_trace','WABBER_PROCESS_ISOLATION':'lambda_microvm' if out.name in {'E1aAwsLambda','E1bAwsLambda'} else 'nested_seccomp','WABBER_APPROVED_TRACE':'approved_trace.json','ATSUITE_EPHEMERAL_WORKSPACE':'true','WABBER_EXECUTION_BACKEND':'native','WABBER_DATA_ROOT':'datasets','WABBER_EVALUATION_MAPPING':'evaluation_alignment.json',
          'WABBER_WORKER_MEMORY_MB':'3072','WABBER_CODE_TIMEOUT':'180','OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1','MKL_NUM_THREADS':'1','NUMEXPR_NUM_THREADS':'1'}
(out/'cloud_settings.json').write_text(json.dumps(settings,indent=2)+'\n')
PY
