"""Opt-in real service acceptance. No paid writes without --confirm-live.

Run separately for each actual service/region/account. Keep original evidence;
failures are not filtered and every execution uses the built-in service/handlers.
"""
import argparse
import json
import time
from pathlib import Path
from atsuite.recommender.contracts import ExperimentSpec
from atsuite.recommender import service
from atsuite.execution.postgres_store import open_store

def acceptance_checks(result):
    gates=(result.get('recommendation') or {}).get('eligibility',[])
    candidates=result.get('evidence_candidates') or []
    artifacts=[a for targets in (result.get('published_artifacts') or {}).values() for a in targets.values()]
    from atsuite.execution.publish import DIGEST
    return {'completed':result['state']=='COMPLETED','cleanup_readback':result.get('cleanup_status')=='success',
        'real_observations':bool(candidates) and all(c['measurement_source']=='platform_observed' for c in candidates),
        'confirmation_evidence':bool(gates) and {g['candidate_id'] for g in gates}=={c['candidate_id'] for c in candidates} and all(g['eligible'] for g in gates),
        'registry_manifests':bool(artifacts) and all(a.get('registry_verified') and DIGEST.fullmatch(str(a.get('digest',''))) for a in artifacts)}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--provider',choices=('aws_lambda','aws_agentcore','gcp_faas','gcp_mcp','ali_fc','ali_agentrun'),required=True)
    parser.add_argument('--region',required=True);parser.add_argument('--account',required=True)
    parser.add_argument('--market-snapshot',type=Path)
    parser.add_argument('--max-spend',type=float,required=True);parser.add_argument('--max-wall-seconds',type=int,required=True)
    parser.add_argument('--confirmation-runs',type=int,default=500);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--confirm-live',action='store_true',help='explicit approval for actual billable cloud execution')
    args=parser.parse_args(argv)
    spec=ExperimentSpec.from_dict({'benchmark':{'config':str(args.config.resolve())},'providers':[args.provider],
        'regions':{args.provider:args.region},'objective':{'type':'min_cost','min_success_rate':.99},
        'search':{'mode':'grid','max_candidates':2},'protocol':{'confirmation_runs':args.confirmation_runs},
        'budget':{'max_estimated_spend':args.max_spend,'max_wall_time_seconds':args.max_wall_seconds,'max_llm_tokens':0},
        'prediction':{'enabled':False},'advisor':{'enabled':False}})
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    snapshot=json.loads(args.market_snapshot.read_text()) if args.market_snapshot else None
    frozen=service.plan(spec,output/'plan.json',market_snapshot=snapshot)
    if not args.confirm_live:
        print(json.dumps({'status':'plan_only','cloud_verified':False,'plan':str(output/'plan.json')}));return 0
    store=open_store(output/'jobs.sqlite')
    try:
        approval=service.approve(store,frozen,subject='cloud-acceptance',tenant='acceptance',project='recommender',accounts={args.provider:args.account})
        record=service.enqueue(store,frozen,approval,tenant='acceptance',project='recommender',subject='cloud-acceptance')
        started=time.monotonic()
        result=service.run(store,record['id'])
        while result['state']=='CLEANUP_PENDING' and time.monotonic()-started<args.max_wall_seconds:
            time.sleep(1)
            result=service.run(store,record['id'])
        checks=acceptance_checks(result)
        accepted=all(checks.values())
        evidence={'schema_version':1,'provider':args.provider,'region':args.region,'experiment_id':record['id'],
            'status':'cloud_verified' if accepted else 'acceptance_failed','cloud_verified':accepted,'checks':checks,
            'plan_digest':service.plan_digest(frozen),'report_path':result.get('report_path'),
            'cleanup_status':result.get('cleanup_status'),'stage_records':result.get('stage_records')}
        (output/'acceptance.json').write_text(json.dumps(evidence,indent=2)+'\n')
        print(json.dumps(evidence));return 0 if accepted else 1
    finally:store.close()
if __name__=='__main__':raise SystemExit(main())
