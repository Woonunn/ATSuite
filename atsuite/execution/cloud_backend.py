"""Default external adapters, reusing ATSuite build, deploy and replay implementations."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
from pathlib import Path
from dataclasses import asdict
from .publish import publish_artifact,require_digest
from atsuite.recommender.identity import digest,file_digest

class CloudBackend:
    simulated=False
    ownership_cleanup=True
    def __init__(self):
        self.predictor=None
        model_id=os.environ.get('ATSUITE_PREDICTOR_MODEL_ID')
        if model_id:
            try:
                from atsuite.prediction.registry import ModelRegistry
                from atsuite.prediction.predictor import Predictor
                self.predictor=Predictor(ModelRegistry(os.environ.get('ATSUITE_MODEL_REGISTRY','models')).load(model_id))
            except Exception:
                self.predictor=None
    @property
    def isolated_builder(self):
        value=os.environ.get('ATSUITE_INIT_IMAGE','')
        try: require_digest(value.rsplit('@',1)[1]); return True
        except (ValueError,IndexError): return False

    def preflight(self,record):
        from atsuite.market_data.service import freeze_market_data,conservative_operation_estimates
        from atsuite.recommender.capabilities import validate_candidate
        from atsuite.recommender.contracts import CandidateSpec
        from atsuite.pipeline import resolve_benchmark
        spec=record['plan']['spec']; candidates=record['plan']['candidates']; accounts={}; oracle=None
        frozen_bases=dict(record.get('frozen_base_images') or {})
        for candidate in candidates:
            fields={name:value for name,value in candidate.items() if name in CandidateSpec.__dataclass_fields__}
            validation=validate_candidate(CandidateSpec.from_dict(fields))
            if not validation.valid: raise ValueError('capability_changed:'+str(validation.reasons))
            provider=candidate['provider']; region=candidate['region']
            if region=='default': raise ValueError('execution requires explicit provider region')
            if provider.startswith('aws_'):
                import boto3
                account=boto3.client('sts',region_name=region).get_caller_identity()['Account']
            elif provider.startswith('gcp_'):
                project=os.environ.get('GOOGLE_CLOUD_PROJECT','')
                if not project: raise RuntimeError('GOOGLE_CLOUD_PROJECT required')
                response=subprocess.run(['gcloud','projects','describe',project,'--format=json'],check=True,capture_output=True,text=True,timeout=30)
                account=json.loads(response.stdout)['projectId']
            elif provider.startswith('ali_'):
                response=subprocess.run(['aliyun','sts','GetCallerIdentity','--RegionId',region],check=True,capture_output=True,text=True,timeout=30)
                account=json.loads(response.stdout)['AccountId']
            else: raise ValueError('default cleanup adapter unavailable:'+provider)
            approved=record['approval']['accounts'].get(provider)
            if not approved or str(approved)!=str(account): raise ValueError('approval_account_mismatch:'+provider)
            accounts[provider]=str(account)
            config=json.loads(Path(candidate['generated_config']).read_text())
            raw=(config.get('correctness') or {}).get('oracle')
            if isinstance(raw,str):
                root=Path(candidate['bundle_root']); path=(root/raw).resolve()
                if root not in path.parents: raise ValueError('oracle escapes frozen bundle')
                raw=json.loads(path.read_text())
            if not isinstance(raw,dict) or raw.get('type')!='exact' or not ('expected_outputs' in raw or 'expected_output' in raw):
                raise ValueError('declared exact output oracle required')
            stateless=all(node.get('state',{}).get('mode')=='stateless' for node in config['nodes'])
            current={**raw,'version':raw.get('version') or 'exact-v1:'+digest(raw,length=32),'state_policy':'declared_stateless' if stateless else 'requires_state'}
            if oracle is not None and oracle!=current: raise ValueError('oracle differs between candidates')
            oracle=current
            if any(Path(node.dir,'init.sh').exists() for node in resolve_benchmark(Path(candidate['generated_config']),provider).nodes.values()) and not self.isolated_builder:
                raise RuntimeError('pinned isolated builder image required for init')
            if not shutil.which('docker'): raise RuntimeError('Docker CLI required for default builder and registry publication')
            if candidate['candidate_id'] not in frozen_bases:
                from .base_images import freeze_candidate_bases
                frozen_bases[candidate['candidate_id']]=freeze_candidate_bases(candidate)
        frozen=record.get('market_snapshot') or record['plan'].get('market_snapshot')
        policy={**spec['market_data'],'pricing':'snapshot'} if frozen else spec['market_data']
        market=freeze_market_data(candidates,policy,approved_accounts=accounts.values(),approved_snapshot=frozen)
        if market.get('status') not in {'frozen','frozen_stale_approved'}:
            raise RuntimeError('market_snapshot_incomplete')
        estimates=conservative_operation_estimates(candidates,market,spec['protocol'])
        return {'accounts':accounts,'market_snapshot':market,'oracle':oracle,'operation_estimates':estimates,
                'frozen_base_images':frozen_bases,
                'execution_environment':{'builder':'resource_limited_containers','measurement_source':'platform_observed'}}

    def build(self,candidate,record):
        from atsuite.cli.build_docker_images import build_resolved_targets
        from .build import build_key
        output=Path(record.get('artifact_root') or Path(candidate['generated_config']).parents[4]/'execution')
        # Build subprocesses inherit only build settings, never deployment keys.
        from .safe_process import run_builder
        return run_builder(candidate,output,experiment_id=record['id'],build_id=build_key(candidate))

    def freeze_child_images(self,candidate):
        from .base_images import freeze_candidate_bases
        return freeze_candidate_bases(candidate)

    def publish(self,artifacts,candidate,record):
        provider=candidate['provider']; account=record['accounts'][provider]; region=candidate['region']
        configured=json.loads(os.environ.get('ATSUITE_REGISTRY_REPOSITORIES','{}'))
        repository=configured.get(provider)
        if not repository:
            if provider.startswith('aws_'):
                repository=f"{account}.dkr.ecr.{region}.amazonaws.com/{os.environ.get('ECR_REPOSITORY_NAME','atsuite')}"
            elif provider.startswith('gcp_'):
                repository=f"{region}-docker.pkg.dev/{account}/{os.environ.get('GCP_ARTIFACT_REPOSITORY','atsuite')}/runtime"
            else: repository=os.environ.get('DOCKERHUB_NAME')
        if not repository: raise RuntimeError('registry repository required')
        # Registry login is explicit SDK/CLI setup; no fallback to local RepoDigest.
        return {target:publish_artifact(artifact,repository) for target,artifact in artifacts.items()}

    def deploy(self,candidate,published,record,intent_callback):
        from atsuite.deploy import deploy
        from .readback import configuration_from_map
        output=Path(candidate['bundle_root']).parent/'deployment'/record['id']
        path=deploy(Path(candidate['generated_config']),candidate['provider'],artifacts=published,experiment_id=record['id'],
            candidate_id=candidate['candidate_id'],namespace=f"{record['tenant']}/{record['project']}",region=candidate['region'],
            on_resource=intent_callback,output_dir=output,ownership=record['ownership'],tenant=record['tenant'],
            state_bucket=json.loads(os.environ.get('ATSUITE_STATE_BUCKETS','{}')).get(candidate['provider'],'atsuite'))
        payload=json.loads(Path(path).read_text())
        result=configuration_from_map(candidate,published,payload)
        return {**result,'url_map':str(path),'ownership':record['ownership'],'candidate_id':candidate['candidate_id']}

    def invoke(self,candidate,deployment,assignment,record):
        from atsuite.invoker import run_trace
        from atsuite.market_data.service import usage_from_report
        output=Path(deployment['url_map']).parent/'runs'
        result=run_trace(Path(candidate['generated_config']),Path(deployment['url_map']),assignment.run_id,
            provider=candidate['provider'],output_dir=output,raise_on_error=False,
            skip_scaling_config=True,llm_time_scale=record['plan']['spec']['protocol']['llm_replay_scale'],
            state_identity={'tenant':record['tenant'],'experiment':record['id'],'candidate':candidate['candidate_id'],'scenario':assignment.scenario_id},
            dispatch_guard=record.get('_dispatch_guard'),
            state_bucket=json.loads(os.environ.get('ATSUITE_STATE_BUCKETS','{}')).get(candidate['provider'],'atsuite'))
        observed=json.loads(Path(result['outputs_path']).read_text())
        report=json.loads(Path(result['report_path']).read_text()) if result.get('report_path') else {}
        physical={r['logical_target']:r['physical_name'] for r in record.get('resource_records',{}).values() if r['candidate_id']==candidate['candidate_id']}
        usage=usage_from_report(report,{**candidate,'resource_ids_by_target':physical},
            {**asdict(assignment),'tenant_id':record['tenant'],'experiment_id':record['id'],'attempt_id':assignment.run_id+':attempt:0'},
            account_id=record['accounts'][candidate['provider']],market_snapshot=record['market_snapshot'])
        summary=report.get('summary') or {}
        memory=summary.get('avg_memory_usage_mb') if summary.get('metric_availability',{}).get('avg_memory_usage_mb')=='available' else None
        return {**result,'outputs':observed,'duration_ms':result.get('summary',{}).get('run_user_e2e_ms'),
            'usage':usage['entries'],'usage_coverage':usage['coverage'],
            'memory_mb':memory,
            'state_validation':'unknown','state_snapshot':next((v['output'] for v in observed.values() if v.get('node_name')==record['oracle'].get('state_output_node')),None),
            'output_validation':'unknown'}

    def cleanup(self,resource,record):
        from .cleanup import cleanup_owned_resource
        return cleanup_owned_resource(resource,record)
