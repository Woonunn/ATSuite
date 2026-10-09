"""Apply bounded model/grid proposals through ordinary materialization and stages."""
from __future__ import annotations
from dataclasses import asdict
import json
import os
from pathlib import Path
from atsuite.advisor.client import create_advisor
from atsuite.advisor.model import CandidateProposal
from atsuite.advisor.workflow import validate_proposal
from atsuite.recommender.identity import digest,file_digest


def approved_search_boundary(plan,approval):
    boundary=dict(approval['search_boundary'])
    full=[*plan.get('candidates',[]),*plan.get('withheld_candidate_proposals',[])]
    boundary['families']=sorted({row['family'] for row in full})
    maxima={}
    for row in full:
        target_max=maxima.setdefault(row['provider'],{})
        for resource in row.get('resources_by_target',{}).values():
            for field,value in resource.items():
                if isinstance(value,(int,float)) and not isinstance(value,bool):
                    target_max[field]=max(target_max.get(field,value),value)
    if not boundary.get('resource_maxima_by_provider'):
        boundary['resource_maxima_by_provider']=maxima
    boundary['topologies']=[row['logical_topology'] for row in full]
    if set(boundary.get('topology_strategies',()))-{'baseline'}:
        boundary['topology_policy']='canonical_validator'
    return boundary


def collect_child_candidates(record,*,backend,budget_controller,artifact_root):
    """Only local artifacts are created here; cloud writes use normal handlers."""
    from atsuite.recommender.api import materialize_child_candidate
    plan=record['plan'];parent_digest=record['approval']['plan_digest']
    boundary=approved_search_boundary(plan,record['approval'])
    capacity=int(boundary['max_candidates'])-len(plan['candidates'])-len(record.get('child_candidates',[]))
    if capacity<=0:return [],[{'status':'no_proposal_capacity','parent_plan_digest':parent_digest}]
    output_root=Path(artifact_root)/record['id']/'children'
    accepted={};audit=[]
    def materialize(spec):
        key=digest(spec,length=64)
        if key in accepted:return accepted[key]
        if len(accepted)>=capacity:
            raise ValueError('proposal_candidate_budget_exceeded')
        path=output_root/key/'plan.json'
        if path.is_file():
            frozen=json.loads(path.read_text())
            if frozen.get('parent_plan_digest')!=parent_digest or frozen.get('proposal_digest')!=key:
                raise ValueError('immutable_child_plan_mismatch')
            child=frozen['candidates'][0]
            if file_digest(Path(child['generated_config']))!=child['generated_config_digest']:
                raise ValueError('immutable_child_configuration_changed')
        else:
            child=materialize_child_candidate(plan,spec,path)
        existing={row['candidate_id'] for row in plan['candidates']}|{row['candidate_id'] for row in record.get('child_candidates',[])}
        if child['candidate_id'] in existing:
            raise ValueError('proposal_duplicates_existing_candidate')
        accepted[key]=child
        return child
    spec=plan['spec']
    catalog={'candidates':{row['candidate_id']:row for row in plan['candidates']},'eligible_candidates':[], 'objective_winner':None,'metrics':{},'decision_status':'exploration',
             'intent':plan.get('intent',{}),'pilot':record.get('pilot_results',{}),'search_boundary':boundary,'parent_plan_digest':parent_digest}
    # Keep pilot numeric data as explicit experiment observations; never call it
    # verified evidence or expose raw logs/code to the model.
    for cid,pilot in record.get('pilot_results',{}).items():
        for name,value in pilot.items():
            if value is not None:
                catalog['metrics'][cid+':pilot:'+name]={'candidate_id':cid,'metric_id':cid+':pilot:'+name,'value':value,'unit':'ms' if name=='latency_ms' else 'ratio','scope':'exploration','quality':'pilot','measurement_kind':'derived'}
    settings=spec.get('advisor',{})
    configured_model=os.environ.get(settings.get('model_from_env','ATSUITE_ADVISOR_MODEL')) or None
    client=getattr(backend,'advisor',None) or create_advisor(provider=settings.get('provider','openai'),model=configured_model,
        enabled=settings.get('enabled',True),budget_controller=budget_controller,proposal_validator=materialize,
        search_boundary=boundary,parent_plan_digest=parent_digest)
    try:
        response=client.recommend(catalog,'Propose one new legal resource or topology configuration inside the supplied frozen search boundary. It will require real pilot and independent confirmation measurements. Do not change objective, permissions, account, or budget.')
        audit.append({'status':'advisor_response','provider':response.provider,'model':response.model,'audit':response.audit,'parent_plan_digest':parent_digest})
        # A response may have materialized validated local artifacts before a
        # later parse/claim failure. Only proposals in the final valid response
        # can be accepted; local orphan files confer no execution authority.
        if not response.valid:raise ValueError('invalid_advisor_proposal_response')
        proposed=[]
        for proposal in response.candidate_proposals:
            if not proposal.specification:continue
            child=validate_proposal(proposal,search_boundary=boundary,parent_plan_digest=parent_digest,validator=materialize)['candidate']
            proposed.append(child)
        accepted={child['proposal_digest']:child for child in proposed}
    except Exception as exc:
        accepted={};audit.append({'status':'advisor_fallback','reason_code':type(exc).__name__,'parent_plan_digest':parent_digest})
    if not accepted:
        for fallback in plan.get('withheld_candidate_proposals',[])[:capacity]:
            try:
                child=validate_proposal(CandidateProposal('grid-fallback',specification=fallback,parent_plan_digest=parent_digest),search_boundary=boundary,parent_plan_digest=parent_digest,validator=materialize)['candidate']
                audit.append({'status':'grid_fallback_child','candidate_id':child['candidate_id'],'parent_plan_digest':parent_digest})
            except ValueError as exc:audit.append({'status':'proposal_rejected','reason_code':str(exc),'parent_plan_digest':parent_digest})
    children=list(accepted.values())
    if len(children)>capacity:raise ValueError('proposal_capacity_exceeded')
    return children,audit
