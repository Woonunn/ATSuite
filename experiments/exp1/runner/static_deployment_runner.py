"""One immutable provider per run, paired request tables, and explicit budget reservations."""
import argparse
import asyncio
import csv
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from dataclasses import fields
import fcntl
import hashlib
import json
from pathlib import Path
import time
import traceback
import yaml
from dotenv import load_dotenv
from experiments.exp1.data.prepare_e1a import ROOT,load_requests,sha
from experiments.exp1.capture.capture_agent_traces import run as replay_agent
from experiments.exp1.runner.arrival_scheduler import Arrival,ms_ns,run_open_loop


def reserve_budget(ledger, config, provider, run_key, amount, basis):
    pool='aws' if provider.startswith('aws_') else 'ali'
    cap=config['budget']['aws_usd_admission_cap' if pool=='aws' else 'ali_cny']
    cap=Decimal(str(cap));fraction=Decimal(str(config['budget']['reserve_fraction']));amount=Decimal(str(amount))
    if not cap.is_finite() or cap<=0 or not fraction.is_finite() or not 0<=fraction<1:raise ValueError('Invalid budget cap or reserve fraction')
    limit=cap*(1-fraction)
    if not amount.is_finite() or amount<=0 or not basis:raise ValueError('A positive conservative reservation and its evidence are required')
    ledger=Path(ledger);ledger.parent.mkdir(parents=True,exist_ok=True)
    with ledger.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        value=json.loads(ledger.read_text()) if ledger.exists() else {'reservations':[]}
        if any(r['run_key']==run_key for r in value['reservations']):raise ValueError('This run already has a budget reservation')
        held=sum((Decimal(str(r['reserved_amount'])) for r in value['reservations'] if r['pool']==pool),Decimal(0))
        if held+amount>limit:raise ValueError(f'Budget admission refused: {held}+{amount}>{limit} {"USD" if pool=="aws" else "CNY"}')
        value['reservations'].append({'pool':pool,'provider':provider,'run_key':run_key,'reserved_amount':str(amount),
            'currency':'USD' if pool=='aws' else 'CNY','basis':basis,'created_epoch':time.time(),
            'actual_charge':None,'status':'planning_reservation_not_actual_billing'})
        temp=ledger.with_suffix('.tmp');temp.write_text(json.dumps(value,indent=2)+'\n');temp.replace(ledger)
    return value['reservations'][-1]


def reassess_reservation(ledger,run_key,amount,basis,evidence):
    """Revise a planning bound from closed usage; this never creates an actual bill."""
    amount=Decimal(str(amount))
    if not amount.is_finite() or amount<=0 or not basis or not evidence:raise ValueError('A positive, evidenced planning bound is required')
    for item in evidence:
        if sha(ROOT/item['path'])!=item['sha256']:raise ValueError('Usage/pricing evidence changed')
    ledger=Path(ledger)
    with ledger.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX);value=json.loads(ledger.read_text())
        row=next(r for r in value['reservations'] if r['run_key']==run_key)
        if row.get('actual_charge') is not None and amount<Decimal(str(row['actual_charge'])):raise ValueError('Cannot reserve less than an already known actual charge')
        if amount>Decimal(row['reserved_amount']):raise ValueError('A larger bound requires fresh budget admission')
        row.setdefault('revisions',[]).append({'previous_amount':row['reserved_amount'],'previous_basis':row['basis'],'previous_evidence':row.get('evidence'),'previous_status':row.get('status'),'epoch':time.time()})
        row.update(reserved_amount=str(amount),basis=basis,evidence=evidence,status='planning_bound_reassessed_from_closed_usage_not_actual_billing')
        temp=ledger.with_suffix('.tmp');temp.write_text(json.dumps(value,indent=2)+'\n');temp.replace(ledger)
    return row


async def execute(workload, deployment, provider, seed, role, output, *, pilot=False, admission=None):
    output=Path(output).resolve();manifest,rows=load_requests(workload,seed,role);config=manifest['config']
    deployed=json.loads(Path(deployment).read_text())
    entry=next(r for r in deployed['results'] if r['provider']==provider)
    if entry['status']!='deployed':raise ValueError('Provider has no verified deployment/resource readback')
    if entry['image'].get('approved_task_id')!=manifest['task_id'] or entry['image'].get('capture_events_sha256')!=manifest['capture_files']['events.jsonl']:raise ValueError('Deployed frozen program does not match the sealed workload')
    url_map=ROOT/'url_results'/Path(entry['config']).name
    endpoints=json.loads(url_map.read_text())
    if endpoints.get('provider')!=provider:raise ValueError('Endpoint map belongs to another provider')
    target_id,endpoint=next(iter(endpoints['targets'].items()))
    if len(endpoints['targets'])!=1 or not endpoint['endpoint']:raise ValueError('E1-A expects one verified static Tool target')
    if pilot:
        rows=rows[:config['pilot_requests']]
        duration_ns=ms_ns(rows[-1]['relative_arrival_ms'])+10**9
        duration_ms=str(duration_ns//1000000)
        amount=Decimal('.20')*len(rows) if provider.startswith('aws_') else Decimal('1.0')*len(rows)
        basis='Pilot reservation, not a bill: bounded request count and 240s Tool RPC limit; actual resources are recorded in deployment readback; planning bound may be reassessed from documented closed usage; actual billing remains pending'
    else:
        if admission is None:raise ValueError('Formal replay requires a successful Pilot and evidence-backed budget admission')
        approved=json.loads(Path(admission).read_text())
        if approved.get('workload_manifest_sha256')!=sha(Path(workload)/'manifest.json') or approved.get('request_table_sha256')!=manifest['request_table_sha256'] or approved.get('pilot_passed') is not True:raise ValueError('Admission belongs to another workload or an unsuccessful Pilot')
        key=f'{seed}:{role}:{provider}';proposal=approved['groups'][key]
        if proposal.get('approved') is not True:raise ValueError('This formal group exceeds the calibrated budget or resource limits')
        if proposal.get('deployed_config_sha256')!=entry['config_sha256']:raise ValueError('Deployment changed after Pilot calibration')
        for evidence in proposal['evidence']:
            if sha(ROOT/evidence['path'])!=evidence['sha256']:raise ValueError('Admission evidence changed')
        duration_ms=config['window_duration_ms'];amount=proposal['upper_cost_reservation'];basis=proposal['basis']
    output.mkdir(parents=True,exist_ok=False)
    run_key=hashlib.sha256(str(output).encode()).hexdigest()
    reservation=reserve_budget(ROOT/'results/phase4/budget_ledger.json',config,provider,run_key,amount,basis)
    (output/'run.json').write_text(json.dumps({'provider':provider,'sequence_seed':seed,'window_role':role,'pilot':pilot,
        'workload':str(Path(workload).resolve()),'request_table_sha256':manifest['request_table_sha256'],
        'deployment':entry,'endpoint_map_sha256':sha(url_map),'reservation':reservation,
        'requests':len(rows),'duration_ms':duration_ms,'started_epoch':time.time(),'source_files':{
            str(p.relative_to(ROOT)):sha(p) for p in [Path(__file__),ROOT/'benchmarks/DataSciBench/langgraph_agent/tool_client.py',ROOT/'experiments/exp1/capture/capture_agent_traces.py',ROOT/'benchmarks/DataSciBench/langgraph_agent/trace_replayer.py',ROOT/'benchmarks/DataSciBench/langgraph_agent/graph.py',ROOT/'atsuite/runtime.py',ROOT/'atsuite/faas/function.py',ROOT/'atsuite/mcp/mcp.py']}},indent=2)+'\n')
    with (output/'planned_requests.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]),lineterminator='\n');writer.writeheader();writer.writerows(rows)
    by_id={r['arrival_id']:r for r in rows};arrivals=[Arrival(**{f.name:r[f.name] for f in fields(Arrival)}) for r in rows]
    graph_config=json.loads((ROOT/'benchmarks/DataSciBench/config/langgraph_experiment.json').read_text())
    graph_config['tool']={'provider':provider,'family':endpoint['family'],'target_id':target_id,'endpoint':endpoint['endpoint'],
        'expected_implementation_sha256':next(value for name,value in entry['image']['source_sha256'].items() if name.endswith('/implementation.py')),
        'timeout':config.get('tool_rpc_timeout_s',240),'execution_scope':'real cloud Tool computation; captured model decisions and recorded model waits'}
    if manifest.get('rendering_equivalence'):graph_config['tool']['approved_renderings']=manifest['rendering_equivalence']['policy']['approved_renderings']
    graph_config['model']['seed']=config['task_seed']
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=config['harness_concurrency']*(graph_config['max_parallel']+1),thread_name_prefix='e1a-rpc'))
    (output/'requests').mkdir()
    async def handler(arrival,started):
        row=by_id[arrival.arrival_id];started()
        try:
            result=await replay_agent(row['task_id'],graph_config,output/'requests'/arrival.arrival_id,ROOT/manifest['capture'])
            return {'status':result['status'],'task_id':row['task_id'],'trace_id':row['trace_id'],'request_directory':str(Path('requests')/arrival.arrival_id)}
        except BaseException:
            # The scheduler retains the traceback; each graph retains all completed Tool events too.
            raise
    summary=await run_open_loop(arrivals,handler,output/'schedule',duration_ms=duration_ms,
        concurrency=config['harness_concurrency'],queue_capacity=config['queue_capacity'],time_scale=1.0,
        metadata={'experiment':'E1-A','provider':provider,'pilot':pilot,'role':role,'seed':seed,
                  'request_table_sha256':manifest['request_table_sha256'],'planned_requests_sha256':sha(output/'planned_requests.csv')})
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--workload',type=Path,required=True);p.add_argument('--deployment',type=Path,required=True)
    p.add_argument('--provider',required=True,choices=['aws_lambda','aws_agentcore','ali_fc','ali_agentrun']);p.add_argument('--seed',type=int,default=11)
    p.add_argument('--role',choices=['low','typical','high'],required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--pilot',action='store_true');p.add_argument('--admission',type=Path)
    a=p.parse_args();load_dotenv(ROOT/'.env');print(json.dumps(asyncio.run(execute(a.workload,a.deployment,a.provider,a.seed,a.role,a.output,pilot=a.pilot,admission=a.admission)),indent=2))
