"""Transparent ranking over the canonical evidence eligibility service."""
from __future__ import annotations
import math
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping
from .contracts import EvidenceMetric, ObjectiveSpec, ProtocolSpec
from .eligibility import ComparisonContext, EvidenceResolver, evaluate_eligibility, required_metrics
from .statistics import quantile_interval, binomial_lower_bound

DEFAULT_ANCHORS = {'p95_workflow_ms': 10000., 'cost': 1., 'failure_rate': 1., 'memory_mb': 32768.}

@dataclass(frozen=True)
class DecisionResult:
    status: str
    best_verified: str | None
    pareto_frontier: tuple[str, ...]
    rankings: tuple[dict[str, Any], ...]
    excluded: tuple[dict[str, Any], ...]
    promising_unverified: tuple[str, ...]
    reason: str
    anchors: dict[str, float]
    eligibility: tuple[dict[str, Any], ...] = ()
    scope: dict[str, Any] | None = None
    def to_dict(self): return asdict(self)

def decide(candidates: Iterable[Mapping[str, Any]], objective: ObjectiveSpec, *,
           anchors: Mapping[str,float] | None = None, minimum_score_margin: float = .02,
           protocol: ProtocolSpec | None = None, context: ComparisonContext | None = None,
           artifact_resolver: EvidenceResolver | None = None) -> DecisionResult:
    rows = list(candidates)
    ids = [str(row.get('candidate_id') or '') for row in rows]
    if not all(ids) or len(set(ids)) != len(ids):
        raise ValueError('duplicate_or_missing_candidate_id')
    run_ids=[t.get('run_id') for row in rows for t in row.get('trials',[])]
    if len(set(run_ids))!=len(run_ids): raise ValueError('duplicate_cross_candidate_run_id')
    objective = ObjectiveSpec.from_dict(asdict(objective))
    fixed = {**DEFAULT_ANCHORS, **dict(anchors or {})}
    if any(not math.isfinite(value) or value <= 0 for value in fixed.values()):
        raise ValueError('decision anchors must be finite and positive')
    if minimum_score_margin < 0 or not math.isfinite(minimum_score_margin):
        raise ValueError('invalid practical difference')
    context = context or ComparisonContext(scenario_id=str(rows[0].get('scenario_id','')) if rows else '',
        workload_fingerprint=str(rows[0].get('workload_fingerprint','')) if rows else '', expected_candidate_ids=tuple(ids))
    weights = {'min_cost': {'cost':1.}, 'min_cost_under_slo':{'cost':1.},
        'min_latency':{'p95_workflow_ms':1.}, 'balanced':{'p95_workflow_ms':.45,'cost':.35,'failure_rate':.2}}[objective.type]
    accepted, excluded, gates, promising = [], [], [], []
    for row in rows:
        gate = evaluate_eligibility(row,objective,protocol,context,artifact_resolver)
        gates.append({'candidate_id':row['candidate_id'], **gate.to_dict()})
        if not gate.eligible:
            excluded.append({'candidate_id':row['candidate_id'], 'reasons':list(gate.blocking_reasons)})
            if any((m.measurement_kind if isinstance(m,EvidenceMetric) else m.get('measurement_kind')) == 'predicted' for m in row.get('metrics',{}).values()):
                promising.append(row['candidate_id'])
            continue
        metrics = {k:(v if isinstance(v,EvidenceMetric) else EvidenceMetric(**v)) for k,v in row['metrics'].items()}
        score = sum(w*float(metrics[k].value)/fixed[k] for k,w in weights.items())
        accepted.append({'candidate_id':row['candidate_id'],'metrics':metrics,'score':score})
    accepted.sort(key=lambda row:(row['score'],row['candidate_id']))
    frontier = tuple(sorted(row['candidate_id'] for row in accepted if not any(other is not row
        and all(other['metrics'][k].value <= row['metrics'][k].value for k in weights)
        and any(other['metrics'][k].value < row['metrics'][k].value for k in weights) for other in accepted)))
    ranks = tuple({'candidate_id':r['candidate_id'],'score':r['score'],'on_pareto_frontier':r['candidate_id'] in frontier} for r in accepted)
    scope = {'requested_candidates':list(context.expected_candidate_ids),'eligible_candidates':len(accepted),
             'search_exhaustive':False,'scenario_id':context.scenario_id,'workload_fingerprint':context.workload_fingerprint}
    unknown = set(context.expected_candidate_ids)-set(ids)
    incomplete = bool(unknown or any(set(g['blocking_reasons'])-set(g['constraint_reasons']) for g in gates))
    if not accepted:
        status = 'insufficient_evidence' if incomplete or not gates else 'no_feasible_candidate'
        reason = 'Missing or unqualified evidence in the declared comparison scope.' if status == 'insufficient_evidence' else 'All fully evidenced candidates violate constraints.'
        winner = None
    elif len(accepted) < 2 or incomplete:
        status, winner, reason = 'insufficient_evidence', None, 'Only verified candidate or incomplete comparison coverage; no comparative winner.'
    else:
        first, second = accepted[:2]
        if objective.type == 'balanced':
            gap = second['score']-first['score']
            bounds=[]
            for item in (first,second):
                row=next(r for r in rows if r['candidate_id']==item['candidate_id'])
                samples=[t['duration_ms'] for t in row['trials'] if t.get('phase')=='confirmation' and t.get('status')=='success' and t.get('output_validation')=='passed']
                interval=quantile_interval(samples,confidence_level=1-(1-(protocol or ProtocolSpec()).confidence_level)/len(rows))
                latency_bounds=(interval.lower,interval.upper)
                cost_metric=item['metrics']['cost']; failure_metric=item['metrics']['failure_rate']
                confirmed=[t for t in row['trials'] if t.get('phase')=='confirmation']
                successes=sum(t.get('status')=='success' and t.get('output_validation')=='passed' for t in confirmed)
                confidence=1-(1-(protocol or ProtocolSpec()).confidence_level)/(2*len(rows))
                failure_lower=binomial_lower_bound(len(confirmed)-successes,len(confirmed),confidence)
                failure_upper=1-binomial_lower_bound(successes,len(confirmed),confidence)
                lower=.45*latency_bounds[0]/fixed['p95_workflow_ms']+.35*cost_metric.ci_lower/fixed['cost']+.2*failure_lower
                upper=.45*latency_bounds[1]/fixed['p95_workflow_ms']+.35*cost_metric.ci_upper/fixed['cost']+.2*failure_upper
                bounds.append((lower,upper))
            if bounds[0][1]>=bounds[1][0]:gap=0
        else:
            name = next(iter(weights)); a = first['metrics'][name]; b = second['metrics'][name]
            gap = (b.value-a.value)/max(abs(a.value),abs(b.value),1e-30)
            # Frozen simultaneous intervals must separate when supplied.
            if a.ci_upper is not None and b.ci_lower is not None and a.ci_upper >= b.ci_lower:
                gap = 0
            if name == 'p95_workflow_ms':
                intervals=[]
                for item in (first,second):
                    observed=next(row for row in rows if row['candidate_id']==item['candidate_id'])
                    samples=[t['duration_ms'] for t in observed['trials'] if t.get('phase')=='confirmation' and t.get('status')=='success' and t.get('output_validation')=='passed']
                    intervals.append(quantile_interval(samples,confidence_level=1-(1-(protocol or ProtocolSpec()).confidence_level)/len(rows)))
                if intervals[0].upper is None or intervals[1].lower is None or intervals[0].upper >= intervals[1].lower:
                    gap = 0
        status = 'no_clear_winner' if gap < minimum_score_margin else 'recommended'
        winner = first['candidate_id'] if status == 'recommended' else None
        reason = 'Objective difference exceeds the declared practical threshold within this scenario and measured scope.' if winner else 'Verified candidates cannot be clearly distinguished.'
    return DecisionResult(status,winner,frontier,ranks,tuple(excluded),tuple(promising),reason,fixed,tuple(gates),scope)
