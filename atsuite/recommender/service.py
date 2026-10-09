"""One control-plane entrypoint for planning, approval, execution and decisions."""
from __future__ import annotations
import copy
import secrets
import time
from pathlib import Path
from dataclasses import asdict
from .contracts import ExperimentSpec, ObjectiveSpec, ProtocolSpec
from .identity import digest
from .eligibility import ComparisonContext, EvidenceResolver


def plan(spec:ExperimentSpec, output:Path|None=None, *, market_snapshot=None):
    from .api import plan_experiment,materialize_plan
    result=plan_experiment(spec)
    import os,json
    configured=os.environ.get('ATSUITE_MARKET_SNAPSHOT')
    if market_snapshot is None and configured:
        market_snapshot=json.loads(Path(configured).read_text())
    if market_snapshot is not None:
        from atsuite.market_data.service import freeze_market_data
        frozen=freeze_market_data(result['candidates'],{**asdict(spec.market_data),'pricing':'snapshot'},approved_snapshot=market_snapshot)
        result['market_snapshot']=frozen
        result['market_snapshot_digest']=digest(frozen,length=64)
    if output is not None: materialize_plan(result,output)
    return result


def plan_digest(plan):
    return digest(plan,length=64)


def approve(store,plan,*,subject,tenant,project,accounts=None):
    spec=ExperimentSpec.from_dict(plan['spec'])
    if plan.get('intent',{}).get('unresolved'):raise ValueError('unresolved_user_intent')
    if spec.execution.production_apply or spec.authorization=='production_apply':
        raise ValueError('experiment approval cannot authorize production_apply')
    if spec.budget.max_estimated_spend is None or spec.budget.max_wall_time_seconds is None:
        raise ValueError('approval requires explicit spend and wall time budgets')
    if not subject or not tenant or not project: raise ValueError('approval identity required')
    resource_maxima={provider:{'cpu':cap['cpu_range'][1],'memory_mb':cap['memory_range'][1],
        'disk_mb':cap['disk_range'][1],'timeout_s':cap['timeout_range'][1]} for provider,cap in plan.get('capabilities',{}).items()}
    approval={'approval_id':'approval-'+secrets.token_hex(16),'plan_digest':plan_digest(plan),'subject':subject,
        'tenant':tenant,'project':project,'accounts':dict(accounts or {}),'regions':dict(spec.regions_by_provider),
        'budget':asdict(spec.budget),'schema_version':2,'created_at':time.time(),'expires_at':time.time()+86400,
        'search_boundary':{'providers':list(spec.providers),'regions':dict(spec.regions_by_provider),
            'max_candidates':spec.search.max_candidates,'topology_strategies':list(spec.search.topology_strategies),
            'families':sorted({c['family'] for c in plan['candidates']}), 'topology_policy':'canonical_validator',
            'resource_maxima_by_provider':resource_maxima,'capability_snapshot_digest':digest(plan.get('capabilities',{}),length=64),
            'architectures_by_provider':{p:list(c['architectures']) for p,c in plan.get('capabilities',{}).items()}}}
    return store.register_approval(approval)


def validate_approval(store,plan,approval,*,tenant,project):
    stored=store.approval(approval.get('approval_id',''))
    if stored != approval or approval['plan_digest'] != plan_digest(plan): raise ValueError('approval_plan_mismatch')
    if (approval['tenant'],approval['project']) != (tenant,project): raise ValueError('approval_identity_mismatch')
    if approval['expires_at'] <= time.time(): raise ValueError('approval_expired')
    return stored


def enqueue(store,plan,approval,*,tenant,project,subject,experiment_id=None):
    validate_approval(store,plan,approval,tenant=tenant,project=project)
    if subject != approval['subject']: raise ValueError('approval_subject_mismatch')
    experiment_id=experiment_id or 'exp-'+digest({'plan':plan_digest(plan),'tenant':tenant,'project':project},length=24)
    try:
        record=store.get(experiment_id)
    except KeyError:
        return store.create(experiment_id,{'plan':copy.deepcopy(plan),'approval':approval,'tenant':tenant,'project':project,
            'subject':subject,'completed_stages':[],'stage_records':{},'decision_status':'pending','execution_status':'queued'},state='APPROVED')
    if record.get('tenant')!=tenant or record.get('project')!=project: raise ValueError('experiment_identity_mismatch')
    if plan_digest(record['plan'])!=plan_digest(plan): raise ValueError('immutable_plan_mismatch')
    if record['state']=='PLANNED': return store.transition(experiment_id,'APPROVED',approval=approval,subject=subject,execution_status='queued')
    return record


def resume(store,experiment_id):
    from .search import SearchOrchestrator
    record=store.get(experiment_id)
    validate_approval(store,record['plan'],record['approval'],tenant=record['tenant'],project=record['project'])
    if record['state']=='CANCELLED':raise ValueError('cancelled experiment requires a new approved execution attempt')
    if record['state']=='FAILED' and record.get('cleanup_status')=='success' and 'ANALYZING' not in record.get('completed_stages',[]):
        deleted={r['candidate_id'] for r in record.get('resource_records',{}).values() if r.get('cleanup_status')=='success'}
        if deleted:
            deployments={cid:value for cid,value in record.get('deployments',{}).items() if cid not in deleted}
            stages=dict(record.get('stage_records',{}))
            if 'DEPLOYING' in stages:stages['DEPLOYING']={**stages['DEPLOYING'],'status':'pending','reconciliation_reason':'owned_resources_deleted'}
            store.checkpoint(experiment_id,'DEPLOYING',{'deployments':deployments,
                'deployment_records':list(deployments.values()),'stage_records':stages,
                'completed_stages':[s for s in record.get('completed_stages',[]) if s!='DEPLOYING'],
                'redeployment_generation':record.get('redeployment_generation',0)+1})
    return SearchOrchestrator(store).resume(experiment_id)


def compare(candidates,objective:ObjectiveSpec,*,protocol:ProtocolSpec|None=None,context=None,artifact_resolver=None):
    from .decision import decide
    result=decide(candidates,objective,protocol=protocol,context=context,artifact_resolver=artifact_resolver).to_dict()
    if context is not None and context.allow_simulated:
        result.update(best_simulated=result['best_verified'],best_verified=None,evidence_quality='simulated',cloud_verified=False)
    return {'schema_version':2,**result,'objective':asdict(objective),'protocol':asdict(protocol or ProtocolSpec())}


def run(store,experiment_id,*,backend=None):
    from atsuite.execution.default_handlers import create_handlers
    from atsuite.execution.jobs import ExperimentJob
    return ExperimentJob(store,experiment_id,create_handlers(store,experiment_id,backend=backend)).run()


def compare_manifest(payload,*,objective=None,base_path=None):
    import json
    from .evidence_adapter import candidate_evidence
    if isinstance(payload,list):
        reports=[]
        for item in payload:
            path=Path(item)
            if not path.is_absolute(): path=Path(base_path or '.')/path
            reports.append(json.loads(path.read_text()))
        rows=[]
        objectives=[report.get('objective') for report in reports if report.get('objective')]
        protocols=[report.get('protocol') for report in reports if report.get('protocol')]
        if any(item!=objectives[0] for item in objectives) or any(item!=protocols[0] for item in protocols):
            raise ValueError('exported_comparison_contract_mismatch')
        for report in reports:
            rows.extend(report.get('evidence_candidates',report.get('candidates',[])))
        payload={'candidates':rows,'objective':objectives[0] if objectives else {},'protocol':protocols[0] if protocols else {}}
    if not isinstance(payload,dict) or not isinstance(payload.get('candidates',payload.get('evidence_candidates')),list):
        raise ValueError('manifest requires evidence candidates or exported report paths')
    rows=payload.get('evidence_candidates',payload.get('candidates'))
    context=ComparisonContext(scenario_id=str(rows[0].get('scenario_id','')) if rows else '',
        workload_fingerprint=str(rows[0].get('workload_fingerprint','')) if rows else '',
        expected_candidate_ids=tuple((payload.get('recommendation',{}).get('scope') or {}).get('requested_candidates') or [r['candidate_id'] for r in rows]),
        allow_simulated=bool(rows) and all(r.get('measurement_source')=='simulated' for r in rows))
    return compare(rows,objective or ObjectiveSpec.from_dict(payload.get('objective')),
        protocol=ProtocolSpec.from_dict(payload.get('protocol')),
        context=context,
        artifact_resolver=EvidenceResolver(payload['artifacts']) if 'artifacts' in payload else None)
