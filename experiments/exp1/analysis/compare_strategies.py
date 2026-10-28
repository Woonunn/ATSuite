"""Paired comparisons and an SLO-constrained oracle, with complete real-cost evidence only."""
import argparse
from decimal import Decimal
import json
import math
from pathlib import Path
from experiments.exp1.data.prepare_e1a import ROOT,sha

PROVIDERS=('aws_lambda','aws_agentcore','ali_fc','ali_agentrun')
CATEGORIES={'runtime','state_storage','state_requests','logs','image_registry','network'}


def reconcile(reports,billing):
    """Each actual vendor line is assigned once; all splits must sum to its full amount."""
    charges=json.loads(Path(billing).read_text())
    if charges.get('basis')!='actual_vendor_billing':raise ValueError('Estimated prices or usage are not actual billing')
    by_key={report['run_key']:report for report in reports};seen=set();allocated={key:[] for key in by_key}
    for line in charges['lines']:
        key=(line['provider'],line['record_id'],line['component'])
        if key in seen:raise ValueError('Duplicate vendor billing line: '+str(key))
        seen.add(key)
        if line['component'] not in CATEGORIES:raise ValueError('Unknown cost component')
        source=ROOT/line['raw_source']
        if sha(source)!=line['raw_source_sha256']:raise ValueError('Raw billing evidence changed')
        amount=Decimal(str(line['amount']))
        if not amount.is_finite():raise ValueError('Non-finite bill amount')
        splits=line['allocations']
        if sum((Decimal(str(s['amount'])) for s in splits),Decimal(0))!=amount:raise ValueError('Billing allocations do not conserve the actual charge')
        for split in splits:
            run_key=split['run_key']
            if run_key not in by_key:raise ValueError('Bill assigned to an unknown run')
            if by_key[run_key]['provider']!=line['provider']:raise ValueError('Cross-provider billing assignment')
            allocated[run_key].append({**{k:line[k] for k in ('provider','record_id','component','currency','raw_source','raw_source_sha256')},
                'amount':str(split['amount']),'allocation_basis':split['basis']})
    for report in reports:
        key=report['run_key'];lines=allocated[key];coverage=charges['coverage'].get(key,{})
        report['cost_breakdown']=lines
        complete=coverage.get('complete') is True and set(coverage.get('components',[]))==CATEGORIES
        if complete and not lines:raise ValueError('An empty bill cannot establish a complete zero charge')
        currencies={line['currency'] for line in lines}
        if len(currencies)>1:raise ValueError('Convert mixed currencies explicitly before totaling')
        report['cost_status']='complete_actual_billing' if complete else 'partial_actual_billing'
        if not complete:continue
        currency=next(iter(currencies));total=sum(Decimal(l['amount']) for l in lines)
        report.update(currency=currency,total_cost=float(total))
        if currency=='CNY':report['total_cost_cny']=float(total)
        elif 'fx_to_cny' in charges:
            fx=charges['fx_to_cny'][currency]
            if sha(ROOT/fx['raw_source'])!=fx['raw_source_sha256']:raise ValueError('FX evidence changed')
            rate=Decimal(str(fx['rate']))
            if not rate.is_finite() or rate<=0:raise ValueError('Invalid FX rate')
            report['total_cost_cny']=float(total*rate);report['fx_basis']=fx
    return reports


def compare(reports):
    groups={}
    for report in reports:
        group=(report['sequence_seed'],report['window_role'],report['request_table_sha256'],report['pilot'])
        values=groups.setdefault(group,{})
        if report['provider'] in values:raise ValueError('Duplicate provider report in paired group')
        values[report['provider']]=report
    comparisons=[]
    for key,values in groups.items():
        missing=sorted(set(PROVIDERS)-set(values));complete=not missing and all(v.get('cost_status')=='complete_actual_billing' and v.get('total_cost_cny') is not None for v in values.values())
        if not missing and len({v['requests'] for v in values.values()})!=1:raise ValueError('Paired providers did not schedule the same request count')
        if not missing and len({v.get('planned_requests_sha256') for v in values.values()})!=1:raise ValueError('Paired logical request sets differ')
        eligible=[v for v in values.values() if v['success_rate']>=.99 and v['slo_attainment']>=.95]
        winner=min(eligible,key=lambda v:v['total_cost_cny'])['provider'] if complete and eligible else None
        comparisons.append({'sequence_seed':key[0],'window_role':key[1],'request_table_sha256':key[2],'pilot':key[3],
            'missing_providers':missing,'actual_billing_complete':complete,'winner':winner,
            'rule':'minimum complete actual cost in CNY, success rate >= 0.99 and SLO attainment >= 0.95; no winner while evidence is incomplete',
            'reason':'complete' if winner else 'no feasible strategy' if complete else 'incomplete paired execution or billing evidence'})
    winners={r['winner'] for r in comparisons if r['winner'] and not r['pilot']}
    return {'comparisons':comparisons,'observed_provider_switch':len(winners)>1 if winners else None,
            'interpretation':'A provider switch is an empirical result, never a window-selection or plotting criterion.'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('runs',nargs='+',type=Path);p.add_argument('--billing',type=Path);p.add_argument('--output',required=True,type=Path)
    a=p.parse_args();reports=[]
    for run in a.runs:
        report=json.loads((run/'window_report.json').read_text());report['run_key']=str(run.resolve().relative_to(ROOT));reports.append(report)
    if a.billing:reports=reconcile(reports,a.billing)
    a.output.mkdir(parents=True,exist_ok=False)
    (a.output/'comparison.json').write_text(json.dumps({**compare(reports),'reports':reports},indent=2,allow_nan=False)+'\n')
