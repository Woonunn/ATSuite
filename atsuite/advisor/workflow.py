"""Typed advisor boundaries used by planning and canonical result explanation."""
from dataclasses import asdict
from atsuite.advisor.intent import parse_intent
from atsuite.recommender.contracts import ObjectiveSpec
from atsuite.recommender.identity import digest


def interpret_request(text, explicit=None):
    intent = parse_intent(text)
    objective = {'type':intent.objective}
    if intent.max_p95_latency_ms is not None:
        objective['max_p95_workflow_ms'] = intent.max_p95_latency_ms
    if intent.min_success_rate is not None:
        objective['min_success_rate'] = intent.min_success_rate
    # Explicit caller constraints take precedence and can only be tightened.
    if explicit:
        for field,value in explicit.items():
            if field == 'max_p95_workflow_ms' and objective.get(field) is not None and value is not None:
                objective[field] = min(value,objective[field])
            elif field == 'min_success_rate' and objective.get(field) is not None and value is not None:
                objective[field] = max(value,objective[field])
            else:
                objective[field] = value
    typed = ObjectiveSpec.from_dict(objective)
    return {'objective':asdict(typed),'allowed_providers':list(intent.allowed_providers),'allowed_families':list(intent.allowed_families),'budget':{'max_estimated_spend':intent.max_cost_usd},'unresolved':list(intent.unresolved),'requires_review':True,'intent_digest':digest(asdict(intent),length=32)}


def validate_proposal(proposal, *, search_boundary, parent_plan_digest, validator, budget_controller=None):
    if proposal.parent_plan_digest != parent_plan_digest:
        raise ValueError('proposal_parent_plan_mismatch')
    spec = dict(proposal.specification)
    if not spec:
        raise ValueError('proposal_has_no_new_specification')
    for field in ('provider','region','family'):
        allowed = search_boundary.get(field+'s')
        if field=='region' and isinstance(allowed,dict):
            value=allowed.get(spec.get('provider'))
            allowed=[value] if isinstance(value,str) else value
        if allowed is not None and spec.get(field) not in allowed:
            raise ValueError('proposal_requires_new_approval:' + field)
    if 'logical_topology' in spec and search_boundary.get('topology_policy')!='canonical_validator' and spec.get('logical_topology') not in search_boundary.get('topologies', []):
        raise ValueError('proposal_requires_new_approval:topology')
    maxima=search_boundary.get('resource_maxima_by_provider',{}).get(spec.get('provider'),search_boundary.get('resource_maxima',{}))
    for resource in spec.get('resources_by_target',{}).values():
        for name,value in resource.items():
            if name in {'architecture','extras'}:
                continue  # The exact ordinary capability/authority validator handles these.
            ceiling=maxima.get(name)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or value < 0:
                raise ValueError('invalid_proposal_resource')
            if ceiling is None or value > ceiling:
                raise ValueError('proposal_requires_new_approval:resource')
    forbidden = {'authorization','permissions','budget','account_id','tenant_id','shell_command','handler_module'}
    if forbidden.intersection(spec):
        raise ValueError('proposal_cannot_expand_authority')
    candidate = validator(spec)  # Exact ordinary capability/topology/state validator.
    if budget_controller is not None:
        budget_controller.reserve(candidates=1)
    return {'parent_plan_digest':parent_plan_digest,'proposal_digest':digest(spec,length=32),'candidate':candidate,'status':'validated_proposal','confirmation_status':'pending'}


def explanation_catalog(candidates, recommendation):
    get=lambda name,default=None:recommendation.get(name,default) if isinstance(recommendation,dict) else getattr(recommendation,name,default)
    catalog={'candidates':{},'metrics':{},'eligible_candidates':[row['candidate_id'] for row in get('rankings',())],'objective_winner':get('best_verified'),'decision_status':get('status','')}
    for candidate in candidates:
        cid = candidate['candidate_id']
        catalog['candidates'][cid] = {'candidate_id':cid,**{k:v for k,v in candidate.items() if k in {'provider','family','region','resources_by_target','logical_topology'}}}
        for name,metric in candidate.get('metrics',{}).items():
            row = asdict(metric) if hasattr(metric,'__dataclass_fields__') else dict(metric)
            reference = cid + ':' + name
            catalog['metrics'][reference] = {**row,'candidate_id':cid,'metric_id':reference}
    return catalog
