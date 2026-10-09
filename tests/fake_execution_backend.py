"""Explicitly simulated external cloud/registry/runtime transport for contract E2E.

Default handlers, workflow DAG execution, correctness, price ledger and ranking
remain real repository code. No simulated evidence is cloud_verified.
"""
import json
from pathlib import Path
from dataclasses import asdict
from datetime import datetime,timezone
from unittest.mock import patch
from atsuite.recommender.identity import digest
from atsuite.pipeline import resolve_benchmark
from atsuite.runtime import _BaseRuntimeAdapter,RuntimeCapabilities,RuntimeSession,InvocationResult

class FakeTransport(_BaseRuntimeAdapter):
    def capabilities(self): return RuntimeCapabilities('faas',False,False,True,'simulated')
    def open_session(self,target,uid): return RuntimeSession(uid,target.target_id)
    def invoke(self,request):
        return InvocationResult(provider_request_id='fake:'+request.call_id,result={'answer':42},client_elapsed_ms=.1)

class FakeExecutionBackend:
    simulated=True
    def __init__(self,*,fail_publish=False,fail_cleanup=False):
        self.calls=[];self.fail_publish=fail_publish;self.fail_cleanup=fail_cleanup
    def preflight(self,record):
        self.calls.append('preflight')
        now=datetime.now(timezone.utc).isoformat()
        card={'snapshot_id':'fake-rate','provider':'aws_lambda','service':'AWSLambda','sku':'test-request','region':'us-east-1',
              'billing_mode':'ondemand','unit':'request','currency':'USD','tiers':[{'up_to':None,'unit_price':'0.0001'}],
              'fetched_at':now,'observed_at':now,'source':'simulated','freshness':'fresh','architecture':'linux/amd64','price_scope':'public'}
        candidate=record['plan']['candidates'][0]
        config=json.loads(Path(candidate['generated_config']).read_text())
        raw=(config.get('correctness') or {}).get('oracle') or {'type':'exact','expected_output':{'answer':42}}
        stateless=all(node.get('state',{}).get('mode')=='stateless' for node in config['nodes'])
        oracle={**raw,'version':'fixture-exact-v1','state_policy':'declared_stateless' if stateless else 'unknown'}
        return {'accounts':{c['provider']:'fake-account' for c in record['plan']['candidates']},'oracle':oracle,
                'market_snapshot':{'snapshot_id':'fake-market','rate_cards':[card],'coverage':'simulated','source':'simulated'},
                'operation_estimates':{'build':'0','deploy':'0','invoke':'.0001','cleanup':'0'}}
    def build(self,candidate,record):
        self.calls.append(('build',candidate['candidate_id']))
        return {target:{'build_id':'build-'+digest(candidate['logical_topology']), 'repository':'local:latest','digest':'',
            'platform':candidate['runtime_architecture']} for target in resolve_benchmark(Path(candidate['generated_config']),candidate['provider']).targets}
    def publish(self,artifacts,candidate,record):
        self.calls.append('publish')
        if self.fail_publish: raise RuntimeError('simulated registry denied')
        return {k:{**v,'repository':'fake.example/runtime','digest':'sha256:'+digest(v,length=64),'registry_verified':True} for k,v in artifacts.items()}
    def deploy(self,candidate,published,record,intent_callback):
        self.calls.append(('deploy',candidate['candidate_id']))
        targets={}
        for target in resolve_benchmark(Path(candidate['generated_config']),candidate['provider']).targets.values():
            physical='at-'+digest({'experiment':record['id'],'candidate':candidate['candidate_id'],'target':target.name},length=40)
            intent_callback({'logical_target':target.name,'physical_name':physical,'provider':candidate['provider'],'region':candidate['region'],'status':'creating'})
            targets[target.name]={'endpoint':'https://simulated.example/'+physical,'family':candidate['family'],'resources':target.runtime.to_dict(),
                                  'configuration_readback':'verified','effective_resources':target.runtime.to_dict()}
            intent_callback({'logical_target':target.name,'physical_name':physical,'provider':candidate['provider'],'region':candidate['region'],'status':'created'})
        path=Path(candidate['bundle_root']).parent/'fake-map.json';path.write_text(json.dumps({'provider':candidate['provider'],'family':candidate['family'],'targets':targets}))
        image=digest({k:v['digest'] for k,v in published.items()},length=64)
        return {'status':'verified','url_map':str(path),'image_digest':'sha256:'+image,'effective_image_digest':'sha256:'+image,
                'effective_config':{name:target['effective_resources'] for name,target in targets.items()},'state_validation':'unknown'}
    def invoke(self,candidate,deployment,assignment,record):
        from atsuite.invoker import run_trace
        self.calls.append(('invoke',assignment.phase,assignment.run_id))
        with patch('atsuite.invoker.create_runtime_adapter',lambda *args:FakeTransport(candidate['provider'])):
            result=run_trace(Path(candidate['generated_config']),Path(deployment['url_map']),assignment.run_id,provider=candidate['provider'],
                output_dir=Path(deployment['url_map']).parent/'runs',raise_on_error=False,skip_analyzer=True,skip_scaling_config=True)
        outputs=json.loads(Path(result['outputs_path']).read_text())
        now=datetime.now(timezone.utc).isoformat()
        usage={'candidate_id':candidate['candidate_id'],'run_id':assignment.run_id,'attempt_id':assignment.run_id+':attempt:0',
            'category':'request','quantity':'1','unit':'request','provider':'aws_lambda','source_ref':result['outputs_path'],
            'service':'AWSLambda','region':'us-east-1','sku':'test-request','architecture':'linux/amd64','billing_mode':'ondemand',
            'price_scope':'public','account_id':'fake-account','billing_period':now[:7],'provider_meter_id':'meter:'+assignment.run_id,'usage_start':now}
        return {'status':result['status'],'outputs':outputs,'duration_ms':result['summary']['run_user_e2e_ms'],
                'usage':[usage],'usage_coverage':[],'state_snapshot':None,'report_ref':result.get('report_path',''),'evidence_ref':result['outputs_path']}
    def cleanup(self,resource,record):
        self.calls.append(('cleanup',resource['physical_name']))
        if self.fail_cleanup: raise RuntimeError('simulated deletion denied')
        return {'cleanup_status':'success','readback':'not_found','simulated':True}
