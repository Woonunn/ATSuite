"""Compare matched arrival stages; never infer composition effects across different times."""
import argparse
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
from experiments.exp1.data.prepare_e1b import load, digest, ROOT, PROVIDERS, sha
from experiments.exp1.runner.result_collector import read_events
from experiments.exp1.runner.arrival_scheduler import stats
from experiments.exp1.analysis.compare_strategies import reconcile


def analyze(workload, runs, billing=None):
    manifest, sealed = load(workload); config = manifest['config']; reports=[]; seen=set()
    for directory in map(Path, runs):
        info=json.loads((directory/'run.json').read_text())
        if info['workload_manifest_sha256'] != sha(Path(workload)/'manifest.json'): raise ValueError('Different experiment manifests')
        key=(info['provider'],info['window_role'],info['scenario'])
        if key in seen: raise ValueError('Duplicate experiment group')
        seen.add(key)
        with (directory/'planned_requests.csv').open() as f: planned=list(csv.DictReader(f))
        expected=[r for r in sealed if r['window_role']==info['window_role'] and r['scenario']==info['scenario'] and float(r['relative_arrival_ms'])<info['duration_ms']]
        if planned!=expected: raise ValueError('Run changed the sealed task assignments or arrivals')
        metrics=read_events(directory/'request_metrics.jsonl'); by_id={r['arrival_id']:r for r in metrics}
        if len(metrics)!=len(by_id) or set(by_id)!={r['arrival_id'] for r in planned}: raise ValueError('Missing or duplicate terminal results')
        for row in planned:
            if any(str(by_id[row['arrival_id']].get(k))!=v for k,v in row.items()): raise ValueError('Metric identities differ from planned requests')
        for stage in sorted({r['stage'] for r in planned},key=int):
            rows=[r for r in planned if r['stage']==stage]; values=[by_id[r['arrival_id']] for r in rows]
            good=[r for r in values if r['status']=='succeeded']; n=len(values)
            reports.append(dict(run_key=str(directory.resolve().relative_to(ROOT))+'#stage='+stage,
                provider=info['provider'],scenario=info['scenario'],window_role=info['window_role'],stage=int(stage),
                validation_only=info['validation_only'], observation_duration_ms=info['duration_ms'], requests=n, success_rate=len(good)/n,
                arrival_signature=digest([(r['arrival_id'],r['relative_arrival_ms']) for r in rows]),
                task_counts=dict(Counter(r['task_id'] for r in rows)),
                slo_attainment=sum(r['end_to_end_ms'] is not None and r['end_to_end_ms']<=config['slo_ms'] for r in good)/n,
                latency_ms_success_only=stats([r['end_to_end_ms'] for r in good if r['end_to_end_ms'] is not None]),
                **{k:stats([r[k] for r in values if r.get(k) is not None]) for k in ('worker_cpu_seconds','worker_peak_rss_bytes','state_synchronization_ms')},
                cost_status='not_applicable_local_validation' if info['validation_only'] else 'pending_vendor_billing',total_cost_cny=None))
    if billing:
        if any(r['validation_only'] for r in reports): raise ValueError('Local validation cannot acquire cloud charges')
        reconcile(reports,billing)
    matched=defaultdict(list)
    for r in reports: matched[(r['window_role'],r['stage'])].append(r)
    for values in matched.values():
        if len({(r['arrival_signature'],r['observation_duration_ms'],r['validation_only']) for r in values})!=1: raise ValueError('Different arrivals in compared stages')
        for r in values:
            control=next((v for v in values if v['provider']==r['provider'] and v['scenario']=='fixed'),None)
            r['p95_delta_vs_matched_control_ms']=None
            r['cost_delta_vs_matched_control_cny']=None
            if control:
                a=r['latency_ms_success_only']['p95']; b=control['latency_ms_success_only']['p95']
                if a is not None and b is not None:r['p95_delta_vs_matched_control_ms']=a-b
                if r['total_cost_cny'] is not None and control['total_cost_cny'] is not None:r['cost_delta_vs_matched_control_cny']=r['total_cost_cny']-control['total_cost_cny']
    oracle=[]
    for role in config['window_roles']:
        for scenario in config['scenarios']:
            for stage in range(len(config['stage_boundaries_ms'])-1):
                values=[r for r in reports if (r['window_role'],r['scenario'],r['stage'])==(role,scenario,stage)]
                complete=set(r['provider'] for r in values)==set(PROVIDERS) and all(not r['validation_only'] and r['cost_status']=='complete_actual_billing' and r['total_cost_cny'] is not None for r in values)
                eligible=[r for r in values if r['success_rate']>=.99 and r['slo_attainment']>=.95]
                winners=[]
                if complete and eligible:
                    minimum=min(r['total_cost_cny'] for r in eligible)
                    winners=sorted(r['provider'] for r in eligible if r['total_cost_cny']==minimum)
                oracle.append(dict(window_role=role,scenario=scenario,stage=stage,winners=winners,complete=complete))
    switches=[]
    for role in config['window_roles']:
        for scenario in config['scenarios']:
            stages=[r for r in oracle if r['window_role']==role and r['scenario']==scenario]
            known=all(r['complete'] and r['winners'] for r in stages)
            switches.append(dict(window_role=role,scenario=scenario,observed_switch=any(not set(a['winners']) & set(b['winners']) for a,b in zip(stages,stages[1:])) if known else None))
    return dict(reports=reports,oracle=oracle,switches=switches,interpretation='Compare scenarios within the same arrival stage and provider. Cross-stage changes alone mix traffic and composition effects. One seed, no multi-seed confidence intervals; overlapping resource labels do not identify a pure CPU or memory causal effect.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--workload',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--billing');p.add_argument('runs',nargs='+')
    a=p.parse_args(); result=analyze(a.workload,a.runs,a.billing)
    with a.output.open('x') as f:f.write(json.dumps(result,indent=2,allow_nan=False)+'\n')
