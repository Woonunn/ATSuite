"""Block bootstrap for attributed cost per correct successful workflow."""
from __future__ import annotations
from collections import defaultdict
import math
import random
from atsuite.recommender.statistics import Interval,quantile


def cost_interval(trials,trial_costs,*,confidence_level=.95,seed=0,resamples=1000):
    if not 0<confidence_level<1 or resamples<100:
        raise ValueError('invalid cost interval policy')
    formal=[t for t in trials if t.get('phase')=='confirmation']
    costs={str(k):float(v) for k,v in trial_costs.items() if v is not None}
    successes=sum(t.get('status')=='success' and t.get('output_validation')=='passed' for t in formal)
    if not formal or successes==0 or any(t['run_id'] not in costs for t in formal):
        return Interval(float('nan'),None,None,confidence_level,'unavailable_usage_lineage')
    if any(not math.isfinite(value) or value<0 for value in costs.values()):
        raise ValueError('invalid observed cost sample')
    estimate=sum(costs[t['run_id']] for t in formal)/successes
    blocks=defaultdict(list)
    for trial in formal:
        if not trial.get('block_id'):
            return Interval(estimate,None,None,confidence_level,'missing_cost_block_identity')
        blocks[trial['block_id']].append(trial)
    if len(blocks)<2:
        return Interval(estimate,None,None,confidence_level,'single_cost_block')
    samples=list(blocks.values());rng=random.Random(seed);ratios=[];unbounded=0
    for _ in range(resamples):
        drawn=[t for _ in range(len(samples)) for t in samples[rng.randrange(len(samples))]]
        successful=sum(t.get('status')=='success' and t.get('output_validation')=='passed' for t in drawn)
        if successful:
            ratios.append(sum(costs[t['run_id']] for t in drawn)/successful)
        else:unbounded+=1
    alpha=(1-confidence_level)/2
    # Zero-success bootstrap samples represent infinite cost, never dropped.
    upper=None if unbounded/resamples>alpha else quantile(ratios,min(1.,(1-alpha)*resamples/len(ratios)))
    lower=quantile(ratios,min(1.,alpha*resamples/len(ratios))) if ratios else None
    return Interval(estimate,lower,upper,confidence_level,'workflow_block_cost_ratio_bootstrap_v1')

def attach_cost_statistics(evidence,cost,*,confidence_level=.95,candidate_count=1,seed=0):
    from dataclasses import asdict
    from atsuite.recommender.identity import digest
    adjusted=1-(1-confidence_level)/max(candidate_count,1)
    interval=cost_interval(evidence.get('trials',[]),cost.get('trial_costs',{}),confidence_level=adjusted,seed=seed)
    metric=evidence['metrics']['cost']
    metric['ci_lower']=interval.lower;metric['ci_upper']=interval.upper
    statistic={key:evidence[key] for key in ('candidate_id','scenario_id','workload_fingerprint') if key in evidence}
    statistic.update(kind='cost_statistics',interval=asdict(interval),trial_costs=cost.get('trial_costs',{}),seed=seed,resamples=1000)
    # Undefined estimates are represented by null in frozen JSON artifacts.
    if not math.isfinite(statistic['interval']['estimate']):statistic['interval']['estimate']=None
    reference='sha256:'+digest(statistic,length=64)
    evidence.setdefault('artifacts',{})[reference]=statistic
    metric['evidence_refs']=[*metric.get('evidence_refs',()),reference]
    evidence['cost_uncertainty']={'method':interval.method,'confidence_level':adjusted,'finite_bounds':interval.lower is not None and interval.upper is not None}
    return evidence
