"""Versioned normalization from complete attempts and immutable usage/pricing artifacts."""
from __future__ import annotations
from dataclasses import asdict
from typing import Any
from .contracts import EvidenceMetric
from .identity import digest
from .statistics import quantile


def candidate_evidence(candidate:dict, trials:list[dict], *, configuration:dict, cost:dict|None=None,
                       ledger:list[dict]|None=None, rate_snapshot:dict|None=None, oracle_version:str='',
                       measurement_source:str='platform_observed', scenario_id:str='default') -> dict:
    identity={'candidate_id':candidate['candidate_id'],'scenario_id':scenario_id,
              'workload_fingerprint':candidate['workload_fingerprint']}
    confirmation=[r for r in trials if r.get('phase')=='confirmation']
    image=configuration.get('image_digest','')
    artifacts={}
    def artifact(kind,**content):
        value={**identity,'kind':kind,**content}; ref='sha256:'+digest(value,length=64); artifacts[ref]=value; return ref
    trial_ref=artifact('confirmation_trials',trials=confirmation)
    config_ref=artifact('configuration',configuration=configuration)
    n=len(confirmation)
    successful=[r for r in confirmation if r.get('status')=='success' and r.get('output_validation')=='passed']
    durations=[r['duration_ms'] for r in successful if r.get('duration_ms') is not None]
    metrics={
        'failure_rate':EvidenceMetric('failure_rate',1-len(successful)/n if n else None,'ratio','workflow',n,
            'derived','available' if n else 'missing',measurement_source,(trial_ref,), 'all_attempts_v1'),
        'p95_workflow_ms':EvidenceMetric('p95_workflow_ms',quantile(durations,.95) if durations else None,'ms','workflow',n,
            'derived','available' if len(durations)==len(successful) and durations else 'missing',measurement_source,(trial_ref,), 'type7_success_v1')}
    memory=[r.get('memory_mb') for r in confirmation]
    import math
    complete_memory=bool(memory) and all(not isinstance(v,bool) and isinstance(v,(int,float)) and math.isfinite(v) and v>=0 for v in memory)
    metrics['memory_mb']=EvidenceMetric('memory_mb',max(memory) if complete_memory else None,'MiB','workflow',n,
        'derived','available' if complete_memory else 'missing',measurement_source,(trial_ref,),'max_observed_workflow_mean_tool_memory_v1')
    refs=[trial_ref]
    if ledger is not None: refs.append(artifact('usage_ledger',entries=ledger))
    if rate_snapshot is not None: refs.append(artifact('rate_snapshot',snapshot=rate_snapshot))
    metrics['cost']=EvidenceMetric('cost',float(cost['amount'])/len(successful) if cost and cost.get('amount') is not None and successful else None,
        cost.get('currency','USD') if cost else 'USD','workflow',n,'derived',
        'estimated' if cost and cost.get('coverage')=='complete' and successful else 'missing',measurement_source,tuple(refs),'ledger_rate_fx_v1')
    semantic_bad=any(r.get('output_validation')=='failed' for r in trials)
    output='failed' if semantic_bad else 'passed' if successful and all(r.get('output_validation')=='passed' for r in successful) else 'unknown'
    state='failed' if any(r.get('state_validation')=='failed' for r in trials) else configuration.get('state_validation','unknown')
    return {**identity,'evidence_schema_version':2,'bundle_digest':candidate.get('bundle_digest',''),
        'config_digest':candidate.get('generated_config_digest',candidate.get('config_digest','')),
        'image_digest':image,'effective_image_digest':configuration.get('effective_image_digest',''),
        'oracle_version':oracle_version,'phase':'confirmation','output_validation':output,'state_validation':state,
        'state_validator':configuration.get('state_validator',''), 'configuration_status':configuration.get('status','unknown'),
        'configuration_ref':config_ref, 'measurement_source':measurement_source,'trials':trials,
        'metrics':{k:asdict(v) for k,v in metrics.items()},'artifacts':artifacts,'cost_basis':'rate_card_estimate',
        'cost_scope':cost.get('cost_scope','declared_runtime_scope') if cost else 'declared_runtime_scope'}
