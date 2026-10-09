"""Built-in durable orchestration; injectable boundaries are cloud APIs, never stages."""
from __future__ import annotations
import copy
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from .budget import BudgetController
from .build import build_key
from .workers import run_parallel
from atsuite.recommender.contracts import ExperimentSpec,ProtocolSpec
from atsuite.recommender.identity import digest,file_digest
from atsuite.recommender.protocol import ExperimentProtocol,Scenario
from atsuite.recommender.evidence_adapter import candidate_evidence
from atsuite.recommender.eligibility import ComparisonContext


def create_handlers(store,experiment_id,*,backend=None):
    from .cloud_backend import CloudBackend
    backend=backend or CloudBackend()
    def checkpoint(record,stage,updates):
        return store.checkpoint(experiment_id,stage,updates,lease_token=record['_lease_token'])
    def budget(record): return BudgetController(store,experiment_id,lease_token=record['_lease_token'])
    def guard(record):
        latest=store.get(experiment_id)
        if latest['state']=='CANCEL_REQUESTED': raise RuntimeError('cancel requested; pending remote work requires reconciliation')
        store.renew(experiment_id,record['_lease_token'])
    def candidates(record):
        rows=[*record['plan']['candidates'],*record.get('child_candidates',[])]
        bases=record.get('frozen_base_images') or {}
        return [{**c,'base_images':bases[c['candidate_id']],'base_image_digest':digest(bases[c['candidate_id']],length=64)}
            if c['candidate_id'] in bases else c for c in rows]
    def preflight(record):
        from atsuite.recommender.service import validate_approval
        validate_approval(store,record['plan'],record['approval'],tenant=record['tenant'],project=record['project'])
        for candidate in candidates(record):
            path=Path(candidate.get('generated_config',''))
            if not path.is_file() or file_digest(path)!=candidate.get('generated_config_digest'):
                raise ValueError('frozen_configuration_changed')
            root=Path(candidate['bundle_root'])
            expected_files={entry['path'] for entry in candidate['bundle_manifest']}
            expected_files.add(path.relative_to(root).as_posix())
            if candidate.get('normalized_source_config'):
                normalized=Path(candidate['normalized_source_config'])
                if normalized.is_symlink() or file_digest(normalized)!=candidate['normalized_source_digest']:
                    raise ValueError('frozen_bundle_changed: normalized source')
                expected_files.add(normalized.relative_to(root).as_posix())
            actual_files={file.relative_to(root).as_posix() for file in root.rglob('*') if file.is_file() or file.is_symlink()}
            if actual_files != expected_files:
                raise ValueError('frozen_bundle_changed: file inventory')
            for entry in candidate['bundle_manifest']:
                file=root/entry['path']
                if file.is_symlink() or not file.is_file() or file_digest(file)!=entry['sha256']:
                    raise ValueError('frozen_bundle_changed')
        result=backend.preflight(record)
        if not result or not result.get('market_snapshot') or not result.get('oracle'):
            raise RuntimeError('preflight requires frozen market data and declared oracle')
        estimates=result.get('operation_estimates')
        if not estimates or set(estimates)!={'build','deploy','invoke','cleanup'} or any(v is None for v in estimates.values()): raise RuntimeError('unknown_budget_estimate')
        result['preflight_status']='passed'
        return result
    def building(record):
        guard(record); groups={}
        for c in candidates(record): groups.setdefault(build_key(c),[]).append(c)
        def build(item):
            key,rows=item; latest=store.get(experiment_id)
            existing=latest.get('build_outputs',{}).get(key)
            if existing: return key,existing
            guard(record)
            controller=budget(record)
            controller.reserve(reservation_id='build:'+key,spend=record['operation_estimates'].get('build','0'),builds=1)
            local=backend.build(rows[0],record)
            if not local: raise RuntimeError('build produced no artifacts')
            guard(record)
            published=backend.publish(local,rows[0],record)
            if not published or set(published)!=set(local): raise RuntimeError('publication target coverage mismatch')
            for artifact in published.values():
                from .publish import require_digest
                require_digest(artifact.get('digest'))
                if not artifact.get('registry_verified') or artifact.get('platform')!=rows[0]['runtime_architecture']:
                    raise RuntimeError('unverified_registry_artifact')
            with store._lock:
                partial=dict(store.get(experiment_id).get('build_outputs') or {})
                partial[key]=published
                checkpoint(record,'BUILDING',{'build_outputs':partial})
            return key,published
        built=dict(run_parallel(sorted(groups.items()),build,max_workers=record['plan']['spec']['execution']['build_workers']))
        return {'build_outputs':built,'published_artifacts':{c['candidate_id']:built[build_key(c)] for c in candidates(record)}}
    def deploying(record):
        guard(record)
        published=dict(record.get('published_artifacts') or {})
        for candidate in candidates(record):
            cid=candidate['candidate_id']
            if cid not in published:
                output=record.get('build_outputs',{}).get(build_key(candidate))
                if not output:raise RuntimeError('candidate has no checkpointed publication:'+cid)
                published[cid]=output
        record['published_artifacts']=published
        checkpoint(record,'DEPLOYING',{'published_artifacts':published})
        def deploy_one(c):
            cid=c['candidate_id']; latest=store.get(experiment_id)
            previous=(latest.get('deployments') or {}).get(cid)
            if previous: return cid,previous
            generation=record.get('redeployment_generation',0)
            guard(record); budget(record).reserve(reservation_id='deploy:'+cid+':generation:'+str(generation),
                spend=record['operation_estimates']['deploy'],candidates=0 if generation else 1)
            ownership=digest({'tenant':record['tenant'],'project':record['project'],'experiment':experiment_id,'candidate':cid},length=64)
            def intent(resource):
                guard(record)
                resource={**resource,'candidate_id':cid,'tenant':record['tenant'],'project':record['project'],
                          'experiment_id':experiment_id,'ownership':ownership,'account_id':record['accounts'][c['provider']],
                          'expires_at':record['approval']['created_at']+record['plan']['spec']['budget']['max_wall_time_seconds']+3600,
                          'cleanup_status':'pending','generation':generation}
                with store._lock:
                    resources=dict(store.get(experiment_id).get('resource_records') or {})
                    key=digest({'ownership':ownership,'target':resource['logical_target']},length=32)
                    resources[key]={**resources.get(key,{}),**resource}
                    checkpoint(record,'DEPLOYING',{'resource_records':resources,'cleanup_required':True})
            deployment=backend.deploy(c,record['published_artifacts'][cid],{**record,'ownership':ownership},intent)
            if not deployment or deployment.get('status') not in {'verified','unverified','partial'}:
                raise RuntimeError('deployment has no observed configuration status')
            with store._lock:
                deployed=dict(store.get(experiment_id).get('deployments') or {})
                deployed[cid]=deployment
                checkpoint(record,'DEPLOYING',{'deployments':deployed})
            return cid,deployment
        deployed=dict(run_parallel(candidates(record),deploy_one,max_workers=record['plan']['spec']['execution']['deploy_workers']))
        return {'deployments':deployed,'deployment_records':list(deployed.values())}
    def measure(record,phase):
        guard(record); spec=ExperimentSpec.from_dict(record['plan']['spec'])
        # A child can be durable before publication/deployment finishes. Resume
        # these ordinary idempotent stages before dispatching any child trial.
        if record.get('child_candidates') and any(
            c['candidate_id'] not in (record.get('published_artifacts') or {}) or
            c['candidate_id'] not in (record.get('deployments') or {})
            for c in record['child_candidates']):
            record.update(building(record));record.update(deploying(record))
        rows=candidates(record); audit=list(record.get('search_audit',[]))
        protocol=ExperimentProtocol(experiment_id,(Scenario('default','frozen default'),),
            warmup_runs=spec.protocol.warmup_runs,exploration_runs=spec.protocol.exploration_runs,confirmation_runs=spec.protocol.confirmation_runs,
            seed=0,confidence_level=spec.protocol.confidence_level)
        trials=list(record.get('trials') or [])
        completed={t['run_id'] for t in trials if t.get('status') in {'success','failed','timeout'}}
        lookup={c['candidate_id']:c for c in rows}
        schedule_log=list(record.get(phase+'_schedule') or [])
        def perform(assignments):
            nonlocal trials
            for assignment in assignments:
                if assignment.run_id in completed: continue
                if asdict(assignment) not in schedule_log: schedule_log.append(asdict(assignment))
                checkpoint(record,record['state'],{phase+'_schedule':schedule_log,'search_audit':audit})
                guard(record); candidate=lookup[assignment.candidate_id]
                controller=budget(record)
                controller.reserve(reservation_id='trial:'+assignment.run_id,spend=record['operation_estimates'].get('invoke','0'),trials=1)
                identity={**asdict(assignment),'attempt_id':assignment.run_id+':attempt:0','workload_fingerprint':candidate['workload_fingerprint'],
                    'image_digest':record['deployments'][candidate['candidate_id']].get('image_digest','')}
                started=next((t for t in trials if t['run_id']==assignment.run_id),None)
                if started and started.get('status')=='running':
                    reconciler=getattr(backend,'reconcile_attempt',None)
                    result=reconciler(started,record) if reconciler else None
                    if result is None: raise RuntimeError('pending_remote_attempt_requires_reconciliation')
                else:
                    trials.append({**identity,'status':'running'})
                    checkpoint(record,record['state'],{'trials':trials})
                    result=backend.invoke(candidate,record['deployments'][candidate['candidate_id']],assignment,{**record,'_dispatch_guard':lambda:guard(record)})
                if not result or result.get('status') not in {'success','failed','timeout'}:
                    raise RuntimeError('invocation boundary returned no completed attempt')
                result={**result,**identity}
                result['usage']=[{**entry,'phase':assignment.phase,'tenant_id':record['tenant'],'experiment_id':experiment_id,
                    'candidate_id':assignment.candidate_id,'run_id':assignment.run_id,'attempt_id':identity['attempt_id']} for entry in result.get('usage',[])]
                from atsuite.recommender.correctness import compare_outputs
                oracle=record['oracle']; outputs=result.get('outputs') or {}
                expected=oracle.get('expected_outputs')
                actual={str(v.get('node_name',k)):v.get('output') for k,v in outputs.items()}
                if result['status']=='success':
                    output_pass=(compare_outputs(expected,actual) if expected is not None else
                        bool(actual) and all(compare_outputs(oracle.get('expected_output'),v) for v in actual.values()))
                    result['output_validation']='passed' if output_pass else 'failed'
                    if not output_pass: result['status']='failed'; result['business_error']='oracle_output_mismatch'
                else: result['output_validation']='unknown' if not result.get('business_error') else 'failed'
                if 'expected_state' in oracle:
                    result['state_validation']='passed' if compare_outputs(oracle['expected_state'],result.get('state_snapshot')) else 'failed'
                elif oracle.get('state_policy')=='declared_stateless': result['state_validation']='not_applicable'
                else: result['state_validation']='unknown'
                trials=[result if t['run_id']==assignment.run_id else t for t in trials]
                checkpoint(record,record['state'],{'trials':trials})
                completed.add(assignment.run_id)
                controller.settle('trial:'+assignment.run_id,observed_spend=result.get('observed_spend'),pending=result.get('usage_pending',False))
        if phase=='exploration':
            from atsuite.prediction.acquisition import select_next_candidates
            from atsuite.recommender.workload import profile_workload
            from atsuite.pipeline import resolve_benchmark
            workload=profile_workload(resolve_benchmark(Path(rows[0]['generated_config']),rows[0]['provider'])) if rows else {}
            predictor=getattr(backend,'predictor',None) if spec.prediction_enabled else None
            observed=[]
            while len(observed)<len(rows):
                pilot={}
                for cid in observed:
                    measurements=[t for t in trials if t['candidate_id']==cid and t['phase']=='exploration']
                    values=[t['duration_ms'] for t in measurements if t.get('duration_ms') is not None]
                    pilot[cid]={'latency_ms':sum(values)/len(values) if values else None,
                        'failure_rate':sum(t['status']!='success' for t in measurements)/len(measurements) if measurements else None}
                selection=select_next_candidates(workload,rows,predictor=predictor,objective=spec.objective,limit=1,pilot=pilot,observed_ids=observed)
                if not selection.candidates:break
                selected=selection.candidates[0];cid=selected['candidate_id'];audit.extend(selection.audit)
                perform(protocol.schedule([cid],phase='warmup'))
                perform(protocol.schedule([cid],phase='exploration'))
                observed.append(cid)
                checkpoint(record,record['state'],{'pilot_results':pilot,'search_audit':audit})
            if spec.advisor_enabled and not record.get('proposal_generation_completed'):
                from atsuite.advisor.execution import collect_child_candidates
                artifact_root=Path(record.get('artifact_root') or Path(rows[0]['generated_config']).parents[4]/'execution')
                # The last pilot update must include the final selected candidate.
                pilot={}
                for cid in observed:
                    measurements=[t for t in trials if t['candidate_id']==cid and t['phase']=='exploration']
                    values=[t['duration_ms'] for t in measurements if t.get('duration_ms') is not None]
                    pilot[cid]={'latency_ms':sum(values)/len(values) if values else None,'failure_rate':sum(t['status']!='success' for t in measurements)/len(measurements) if measurements else None}
                record['pilot_results']=pilot
                children,proposal_audit=collect_child_candidates(record,backend=backend,budget_controller=budget(record),artifact_root=artifact_root)
                audit.extend(proposal_audit)
                if children:
                    bases=dict(record.get('frozen_base_images') or {})
                    for child in children:
                        freezer=getattr(backend,'freeze_child_images',None)
                        if freezer is None and not getattr(backend,'simulated',False):
                            raise RuntimeError('child_requires_frozen_base_images')
                        if freezer is not None:bases[child['candidate_id']]=freezer(child)
                    record.update(child_candidates=[*record.get('child_candidates',[]),*children],frozen_base_images=bases)
                    checkpoint(record,record['state'],{'child_candidates':record['child_candidates'],'frozen_base_images':bases,'search_audit':audit})
                    # Ordinary stages skip already completed artifacts/deployments;
                    # the persistent experiment state remains EXPLORING.
                    record.update(building(record));record.update(deploying(record))
                    rows=candidates(record);lookup.update({c['candidate_id']:c for c in rows})
                    for child in children:
                        perform(protocol.schedule([child['candidate_id']],phase='warmup'))
                        perform(protocol.schedule([child['candidate_id']],phase='exploration'))
                checkpoint(record,record['state'],{'proposal_generation_completed':True,'search_audit':audit,'pilot_results':pilot})
        else:
            perform(protocol.schedule([c['candidate_id'] for c in rows],phase=phase))
        return {'trials':trials,'search_audit':audit,phase+'_status':'measured',**({'child_candidates':record.get('child_candidates',[])} if phase=='exploration' else {})}
    def analyzing(record):
        from atsuite.market_data.service import price_candidate_ledger
        from atsuite.recommender.service import compare
        spec=ExperimentSpec.from_dict(record['plan']['spec'])
        evidence=[]
        ledger=[{**entry,'phase':t['phase']} for t in record.get('trials',[]) for entry in t.get('usage',[])]
        priced=price_candidate_ledger(ledger,record['market_snapshot'],target_currency=spec.objective.base_currency)
        costs_by_candidate={row['candidate_id']:row for row in priced['candidate_costs']}
        for c in candidates(record):
            trials=[t for t in record.get('trials',[]) if t['candidate_id']==c['candidate_id']]
            confirmation=[entry for entry in ledger if entry['candidate_id']==c['candidate_id'] and entry['phase']=='confirmation']
            cost=dict(costs_by_candidate.get(c['candidate_id'],{'amount':None,'coverage':'missing','currency':spec.objective.base_currency,'cost_scope':'declared_runtime_scope'}))
            cost['coverage']='complete' if cost.get('coverage')=='available' else 'missing'
            cost['trial_costs']={t['run_id']:priced['trial_costs'].get(t['run_id']) for t in trials if t['phase']=='confirmation'}
            if any(t.get('usage_coverage') for t in trials if t['phase']=='confirmation'):
                cost.update(amount=None,coverage='missing')
            deployment=record['deployments'][c['candidate_id']]
            oracle=record['oracle']
            if oracle.get('state_policy')=='declared_stateless':
                deployment={**deployment,'state_validation':'not_applicable','state_validator':'declared_stateless_v1'}
            elif 'expected_state' in oracle:
                deployment={**deployment,'state_validation':'passed' if trials and all(t.get('state_validation')=='passed' for t in trials) else 'unknown'}
            row=candidate_evidence(c,trials,configuration=deployment,cost=cost,ledger=confirmation,
                rate_snapshot=record['market_snapshot'],oracle_version=oracle['version'],
                measurement_source='simulated' if getattr(backend,'simulated',False) else 'platform_observed')
            from atsuite.market_data.statistics import attach_cost_statistics
            evidence.append(attach_cost_statistics(row,cost,confidence_level=spec.protocol.confidence_level,candidate_count=len(candidates(record))))
        objective=spec.objective
        context=ComparisonContext(scenario_id='default',workload_fingerprint=evidence[0]['workload_fingerprint'] if evidence else '',
            expected_candidate_ids=tuple(c['candidate_id'] for c in candidates(record)),allow_simulated=bool(getattr(backend,'simulated',False)))
        decision=compare(evidence,objective,protocol=spec.protocol,context=context)
        if getattr(backend,'simulated',False):
            decision.setdefault('best_simulated',decision['best_verified']); decision['best_verified']=None
            decision['evidence_quality']='simulated';decision['cloud_verified']=False
        from atsuite.advisor.workflow import explanation_catalog
        from atsuite.advisor.client import create_advisor
        from atsuite.advisor.verify import verify_response
        catalog=explanation_catalog(evidence,decision)
        advisor=create_advisor(provider=spec.advisor_provider,model=__import__('os').environ.get(spec.advisor_model_env) or None,
            enabled=spec.advisor_enabled,budget_controller=budget(record))
        response=advisor.recommend(catalog)
        response=verify_response(response,candidate_catalog=catalog['candidates'],eligible_ids={g['candidate_id'] for g in decision['eligibility'] if g['eligible']},
            metric_catalog=catalog['metrics'],objective_winner=decision['best_verified'])
        explanation=asdict(response)
        root=Path(record.get('artifact_root') or Path(record['plan']['candidates'][0]['generated_config']).parents[4]/'execution')/experiment_id
        root.mkdir(parents=True,exist_ok=True)
        ledger_content=''.join(json.dumps(entry,sort_keys=True)+'\n' for entry in ledger)
        ledger_path=root/('usage_ledger-'+digest(ledger,length=64)+'.jsonl')
        if ledger_path.exists() and ledger_path.read_text()!=ledger_content:
            raise RuntimeError('immutable_usage_ledger_collision')
        if not ledger_path.exists(): ledger_path.write_text(ledger_content)
        report={'schema_version':2,'objective':asdict(objective),'protocol':asdict(spec.protocol),'evidence_candidates':evidence,
            'recommendation':decision,'usage_ledger':str(ledger_path),'market_snapshot':record['market_snapshot'],
            'explanation':explanation,'cloud_verified':False,'evidence_quality':'simulated' if getattr(backend,'simulated',False) else 'platform_observed'}
        report['report_digest']=digest(report,length=64)
        report_path=root/('comparison-'+report['report_digest']+'.json')
        report_content=json.dumps(report,indent=2,allow_nan=False,default=str)+'\n'
        if report_path.exists() and report_path.read_text()!=report_content:
            raise RuntimeError('immutable_comparison_report_collision')
        if not report_path.exists(): report_path.write_text(report_content)
        return {'comparison':report,'recommendation':decision,'decision_status':decision['status'],'explanation':explanation,
            'usage_ledger':str(ledger_path),'report_path':str(report_path),'evidence_candidates':evidence}
    def cleanup(record):
        resources=dict(record.get('resource_records') or {}); pending=[]
        for key,resource in resources.items():
            if resource.get('cleanup_status')=='success': continue
            budget(record).reserve(reservation_id='cleanup:'+key,spend=record.get('operation_estimates',{}).get('cleanup','0'),cleanup=True)
            result=backend.cleanup(resource,record)
            resources[key]={**resource,**result}
            checkpoint(record,'CLEANUP_PENDING',{'resource_records':resources})
            if result.get('cleanup_status')!='success': pending.append(key)
        return {'resource_records':resources,'cleanup_status':'pending' if pending else 'success','cleanup_pending':pending}
    return {'PREFLIGHT':preflight,'BUILDING':building,'DEPLOYING':deploying,'EXPLORING':lambda r:measure(r,'exploration'),
            'CONFIRMING':lambda r:measure(r,'confirmation'),'ANALYZING':analyzing,'CLEANUP':cleanup}
