"""Freeze one authenticated Capture and paired, unscaled Copilot request tables."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import random
from decimal import Decimal
import subprocess
import yaml
from experiments.exp1.runner.validate_arrivals import load_span
from experiments.exp1.runner.arrival_scheduler import ms_ns

ROOT=Path(__file__).resolve().parents[3]
CONFIG=ROOT/'experiments/exp1/configs/e1a.yaml'


def sha(path):
    with Path(path).open('rb') as source:return hashlib.file_digest(source,'sha256').hexdigest()


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def prepare(config, output):
    if config['time_scale']!=1.0:raise ValueError('E1-A requires the original time axis')
    if config['composition']!={config['task_id']:1.0}:raise ValueError('E1-A fixes one task with probability one')
    if len(set(config['sequence_seeds']))!=len(config['sequence_seeds']):raise ValueError('Duplicate sequence seeds')
    if config['task_seed']!=0:raise ValueError('The selected Capture was recorded with task seed 0; do not change replay parameters')
    fx=config.get('budget',{}).get('fx_reference')
    if fx:
        if sha(ROOT/fx['path'])!=fx['sha256']:raise ValueError('Budget FX reference changed')
        currency=json.loads((ROOT/fx['path']).read_text())
        if sha(ROOT/currency['raw_path'])!=currency['raw_sha256']:raise ValueError('Original FX evidence changed')
        if Decimal(str(config['budget']['aws_usd_admission_cap']))*Decimal(currency['usd_to_cny'])>Decimal(str(config['budget']['aws_cny'])):raise ValueError('USD admission ceiling exceeds the CNY budget')
    source=ROOT/'benchmarks/DataSciBench/trace/captured/manifest.json'
    record=next(t for t in json.loads(source.read_text())['captures'] if t['task_id']==config['task_id'])
    capture=ROOT/record['capture']
    for name,expected in record['files'].items():
        if sha(capture/name)!=expected:raise ValueError('Capture checksum mismatch: '+name)
    if json.loads((capture/'result.json').read_text())['status']!='complete':raise ValueError('Capture did not complete')
    task=next(t for t in json.loads((ROOT/'benchmarks/DataSciBench/tasks/manifest.json').read_text())['tasks'] if t['task_id']==config['task_id'])
    files=task['readiness']['inputs']['files']
    for entry in files:
        if sha(ROOT/entry['path'])!=entry['sha256']:raise ValueError('Dataset checksum mismatch: '+entry['name'])
    dataset_version=digest([{k:f[k] for k in ('name','sha256','bytes')} for f in files])
    rendering=None
    if config.get('rendering_equivalence'):
        policy_path=ROOT/config['rendering_equivalence'];policy=json.loads(policy_path.read_text())
        if policy['task_id']!=config['task_id'] or policy['capture_events_sha256']!=sha(capture/'events.jsonl'):raise ValueError('Rendering calibration belongs to another Capture')
        for evidence in policy['evidence']:
            if sha(ROOT/evidence['path'])!=evidence['sha256']:raise ValueError('Rendering validation evidence changed')
        rendering={'path':config['rendering_equivalence'],'sha256':sha(policy_path),'policy':policy}
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    rows=[];groups=[]
    for seed in config['sequence_seeds']:
        rng=random.Random(seed)
        for role in config['window_roles']:
            arrivals,metadata=load_span(ROOT/config['traffic'],role,0,config['window_duration_ms'])
            if int(metadata['selected_window']['duration_ms'])!=config['window_duration_ms']:raise ValueError('Window duration differs from the preselected traffic window')
            providers=list(config['providers']);rng.shuffle(providers)
            groups.append({'sequence_seed':seed,'window_role':role,'provider_order':providers,'requests':len(arrivals),'metadata':metadata})
            for arrival in arrivals:
                row=vars(arrival).copy()
                row.update(arrival_id=digest([config['experiment'],seed,role,arrival.arrival_id])[:32],
                    source_arrival_id=arrival.arrival_id,prompt_id=task['task_id'],task_id=task['task_id'],
                    trace_id=capture.name,trace_sha256=sha(capture/'events.jsonl'),data_version=dataset_version,
                    prompt_sha256=task['prompt_sha256'],sequence_seed=seed,task_seed=config['task_seed'],window_role=role)
                ms_ns(row['relative_arrival_ms']);rows.append(row)
    if not rows:raise ValueError('No eligible arrivals')
    with (output/'requests.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]),lineterminator='\n');writer.writeheader();writer.writerows(rows)
    manifest={'schema_version':1,'experiment':'E1-A','rendering_equivalence':rendering,'config':config,'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
              'task_id':task['task_id'],'capture':record['capture'],'capture_files':record['files'],'dataset_files':files,'data_version':dataset_version,
              'request_table_sha256':sha(output/'requests.csv'),'requests':len(rows),'groups':groups,
              'sampling_rule':f'P(task={config["task_id"]}, trace=selected valid capture)=1; sequence seeds randomize provider order only; captured computation seed stays 0',
              'formal_time_scale':1.0,'traffic_scope':'Sampled production Sessions; absolute rate is not full-product QPS'}
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest


def load_requests(directory,seed,role):
    directory=Path(directory);manifest=json.loads((directory/'manifest.json').read_text())
    if sha(directory/'requests.csv')!=manifest['request_table_sha256']:raise ValueError('Paired request table changed')
    fx=manifest['config'].get('budget',{}).get('fx_reference')
    if fx and sha(ROOT/fx['path'])!=fx['sha256']:raise ValueError('Sealed budget FX reference changed')
    rendering=manifest.get('rendering_equivalence')
    if rendering and sha(ROOT/rendering['path'])!=rendering['sha256']:raise ValueError('Sealed rendering policy changed')
    capture=ROOT/manifest['capture']
    for name,expected in manifest['capture_files'].items():
        if sha(capture/name)!=expected:raise ValueError('Pinned Capture changed')
    rows=[r for r in csv.DictReader((directory/'requests.csv').open()) if int(r['sequence_seed'])==seed and r['window_role']==role]
    if not rows or len({r['arrival_id'] for r in rows})!=len(rows):raise ValueError('Invalid request group')
    return manifest,rows


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',type=Path,default=CONFIG);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();result=prepare(yaml.safe_load(a.config.read_text()),a.output);print(json.dumps({'requests':result['requests'],'sha256':result['request_table_sha256']}))
