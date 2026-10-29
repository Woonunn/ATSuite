"""Admit fixed E1-A groups from successful real Pilots and explicit planning bounds, never invoices."""
import argparse
from collections import deque
from decimal import Decimal,ROUND_CEILING
import json
import math
import shutil
from pathlib import Path
from experiments.exp1.data.prepare_e1a import ROOT,sha,load_requests
from experiments.exp1.runner.result_collector import read_events
from atsuite.tracing.storage import trace_path


def peak_arrivals(rows,span_ms):
    if not math.isfinite(float(span_ms)) or span_ms<=0:raise ValueError('Positive finite service span required')
    queue=deque();peak=0
    for row in rows:
        now=Decimal(row['relative_arrival_ms'])
        while queue and queue[0]<=now-Decimal(str(span_ms)):queue.popleft()
        queue.append(now);peak=max(peak,len(queue))
    return peak


def money(value):
    value=Decimal(str(value))
    if not value.is_finite() or value<0:raise ValueError('Planning amounts must be finite and nonnegative')
    return str(value.quantize(Decimal('.000001'),rounding=ROUND_CEILING))


def load_pilot(path,manifest):
    path=Path(path);info=json.loads((path/'run.json').read_text());report=json.loads((path/'window_report.json').read_text())
    if not info['pilot'] or info['request_table_sha256']!=manifest['request_table_sha256'] or report['successes']!=report['requests'] or report['requests']<3:raise ValueError('Three complete, matching real Pilot Sessions are required')
    if report['latency_ms_success_only']['max']>manifest['config']['slo_ms']:raise ValueError('Pilot already exceeds the fixed SLO')
    records=read_events(path/'request_metrics.jsonl');events={r['arrival_id']:read_events(path/'requests'/r['arrival_id']/'events.jsonl') for r in records}
    uids=[next(e['uid'] for e in values if e['kind']=='tool_session_open') for values in events.values()]
    if len(set(uids))!=len(uids):raise ValueError('Pilot reused an Agent workspace')
    starts=[]
    for values in events.values():
        begin=next(e['monotonic_ns'] for e in values if e['kind']=='run_start');opened=next(e['monotonic_ns'] for e in values if e['kind']=='tool_session_open');starts.append((opened-begin)/1e9)
        for e in values:
            if e['kind']=='tool' and e['tool_name']=='execute_analysis':
                expected=info['provider'] in {'aws_lambda','ali_fc'}
                if e['result']['state_lifecycle']['canonical_restore_performed'] is not expected:raise ValueError('Incorrect provider state lifecycle')
    return info,report,records,events,max(starts)


def calibration(workload,deployment,pilots,rates,output,agentcore_usage=None):
    workload=Path(workload);manifest=json.loads((workload/'manifest.json').read_text());deploy=json.loads(Path(deployment).read_text());prices=json.loads(Path(rates).read_text())
    if prices['basis']!='conservative_planning_rates_not_actual_billing':raise ValueError('Planning prices require an explicit non-billing label')
    for key in ('lambda_gb_second_upper_usd','agentcore_vcpu_hour_upper_usd','agentcore_gb_hour_upper_usd','ali_cu_upper_cny','ali_session_tail_bound_s','ali_faas_release_tail_s'):
        if not math.isfinite(float(prices[key])) or prices[key]<=0:raise ValueError('Invalid planning rate/limit: '+key)
    evidence=[Path(rates),workload/'manifest.json',Path(deployment)];upper={};details={}
    for provider,path in pilots.items():
        info,report,records,events,init_s=load_pilot(path,manifest);entry=next(r for r in deploy['results'] if r['provider']==provider)
        if entry['status']!='deployed' or info['deployment']['config_sha256']!=entry['config_sha256']:raise ValueError('Pilot resources differ from the formal deployment')
        path=Path(path);evidence += [path/'run.json',path/'window_report.json',path/'request_metrics.jsonl',*(trace_path(directory/'events.jsonl') for directory in (path/'requests').iterdir())]
        source=next((path/name for name in ('provider_evidence.json','provider_evidence-early.json') if (path/name).exists()),None)
        index=path/'provider_collection_status.json'
        if index.exists():
            latest=json.loads(index.read_text())
            if latest.get('status')=='collected':source=ROOT/latest['path']
        if source is None:raise ValueError('Missing actual provider usage evidence: '+provider)
        vendor=json.loads(source.read_text());evidence.append(source);by_call={m['invocation_call_id']:m['fields'] for m in vendor['metrics'] if m.get('invocation_call_id')}
        sums=[]
        if provider in {'aws_lambda','ali_fc'}:
            for values in events.values():
                total=0
                for event in values:
                    if event['kind']!='tool':continue
                    fields=by_call.get(event['transport']['request_id'],{})
                    key='billed_duration_ms' if provider=='aws_lambda' else 'duration_ms'
                    value=fields.get(key)
                    if value is None or str(value)=='null':raise ValueError('Missing real billed/execution duration; cannot calibrate '+provider)
                    total+=float(value)/1000
                    if provider=='ali_fc' and str(fields.get('is_cold_start')).lower()=='true':
                        cold=fields.get('cold_start_latency_ms')
                        if cold is None or str(cold)=='null':raise ValueError('Missing cold-start duration')
                        total+=float(cold)/1000
                sums.append(total)
        if provider=='aws_lambda':upper[provider]=max(sums)*manifest['config']['resources']['memory']/1024*prices['lambda_gb_second_upper_usd']*1.25
        elif provider=='aws_agentcore':
            usage_path=Path(agentcore_usage) if agentcore_usage else source;usage=json.loads(usage_path.read_text());evidence.append(usage_path)
            rows=[m['fields'] for m in usage['metrics'] if m['source']=='agentcore_usage']
            if len(rows)<3 or any(r.get('vcpu_hours') is None or r.get('memory_gb_hours') is None for r in rows):raise ValueError('AgentCore usage delivery is still pending')
            upper[provider]=max(r['vcpu_hours']*prices['agentcore_vcpu_hour_upper_usd']+r['memory_gb_hours']*prices['agentcore_gb_hour_upper_usd'] for r in rows)*1.25
            details[provider]={'usage_source':str(usage_path),'note':'If a preceding successful Pilot is supplied, provider-managed physical resources and all program/data/dependency parameters must be unchanged; this is a planning proxy only.'}
        elif provider=='ali_fc':upper[provider]=max(sums)*manifest['config']['resources']['cpu']*prices['ali_cu_upper_cny']
        else:
            active_s=max(r['active_tool_path_ms'] for r in records)/1000+init_s
            span_s=max(r['end_to_end_ms'] for r in records)/1000
            cu=active_s*manifest['config']['resources']['cpu']+manifest['config']['resources']['memory']/1024*.15*(span_s+prices['ali_session_tail_bound_s'])+9*.0075
            upper[provider]=cu*prices['ali_cu_upper_cny']
        details.setdefault(provider,{})['runtime_or_cpu_upper_per_agent']=upper[provider]
        details[provider]['pilot_max_tool_path_ms']=max(r['active_tool_path_ms'] for r in records)
    if set(pilots)!=set(manifest['config']['providers']):raise ValueError('All four real Pilots are required')
    groups={};totals={'aws':Decimal(0),'ali':Decimal(0)}
    for group in manifest['groups']:
        seed=group['sequence_seed'];role=group['window_role'];_,rows=load_requests(workload,seed,role);n=len(rows)
        for provider in group['provider_order']:
            pool='aws' if provider.startswith('aws_') else 'ali';amount=upper[provider]*n
            detail={**details[provider],'agents':n}
            if provider=='ali_fc':
                # Forecast only: warm instances can outlive active request concurrency; this is not a configured instance cap.
                service_ms=details[provider]['pilot_max_tool_path_ms']*1.25
                pool_estimate=min(manifest['config']['harness_concurrency'],2*peak_arrivals(rows,service_ms))
                horizon=(manifest['config']['window_duration_ms']+service_ms)/1000+prices['ali_faas_release_tail_s']
                memory=pool_estimate*manifest['config']['resources']['memory']/1024*.15*horizon*prices['ali_cu_upper_cny']
                amount+=memory;detail.update(warm_pool_planning_estimate=pool_estimate,memory_lifetime_upper_cny=memory)
            ancillary=n*prices['ancillary_per_agent_'+('usd' if pool=='aws' else 'cny')]+prices['ancillary_per_window_'+('usd' if pool=='aws' else 'cny')]
            amount+=ancillary;amount=money(amount);totals[pool]+=Decimal(amount)
            entry=next(r for r in deploy['results'] if r['provider']==provider)
            groups[f'{seed}:{role}:{provider}']={'approved':True,'upper_cost_reservation':amount,'currency':'USD' if pool=='aws' else 'CNY','basis':'Real Pilot usage/execution plus published upper planning rates and explicit cold/idle/ancillary margins; forecast, not an invoice','details':detail,'deployed_config_sha256':entry['config_sha256']}
    output=Path(output)
    if output.exists():raise FileExistsError('Preserve earlier admission evidence')
    snapshots=output.with_suffix('').with_name(output.stem+'-evidence');snapshots.mkdir(exist_ok=False)
    proof=[]
    for source in dict.fromkeys(evidence):
        digest=sha(source);target=snapshots/(digest[:20]+'-'+source.name)
        if not target.exists():shutil.copyfile(source,target)
        assert sha(target)==digest,'Evidence changed during calibration'
        proof.append({'path':str(target.resolve().relative_to(ROOT)),'sha256':digest,'original_path':str(source)})
    ledger=json.loads((ROOT/'results/phase4/budget_ledger.json').read_text())
    budget=manifest['config']['budget'];admitted=True
    for pool,key in [('aws','aws_usd_admission_cap'),('ali','ali_cny')]:
        held=sum(Decimal(r['reserved_amount']) for r in ledger['reservations'] if r['pool']==pool)
        limit=Decimal(str(budget[key]))*(1-Decimal(str(budget['reserve_fraction'])))
        if held+totals[pool]>limit:admitted=False
        details[pool+'_budget']={'held_planning_bounds':str(held),'formal_projection':str(totals[pool]),'admission_limit':str(limit),'within_limit':held+totals[pool]<=limit}
    for proposal in groups.values():proposal.update(approved=admitted,evidence=proof)
    result={'basis':'planning_admission_not_actual_billing','workload_manifest_sha256':sha(workload/'manifest.json'),'request_table_sha256':manifest['request_table_sha256'],'pilot_passed':True,'all_groups_admitted':admitted,'groups':groups,'calibration':details,'actual_charges':None}
    output=Path(output)
    if output.exists():raise FileExistsError('Preserve earlier admission evidence')
    output.write_text(json.dumps(result,indent=2)+'\n');return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--workload',required=True,type=Path);p.add_argument('--deployment',required=True,type=Path);p.add_argument('--rates',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    p.add_argument('--pilot',action='append',required=True,help='provider=run_directory');p.add_argument('--agentcore-usage',type=Path)
    a=p.parse_args();result=calibration(a.workload,a.deployment,dict(x.split('=',1) for x in a.pilot),a.rates,a.output,a.agentcore_usage)
    print(json.dumps({'admitted':result['all_groups_admitted'],'budgets':{k:v for k,v in result['calibration'].items() if k.endswith('_budget')}},indent=2))
