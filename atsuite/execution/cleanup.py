"""Ownership-checked cleanup and resumable TTL reconciliation; never delete shared stores."""
from __future__ import annotations
import hashlib
import json
import subprocess
from .publish import require_digest
from atsuite.recommender.identity import digest


AGENTCORE_USAGE_LOG_GROUP_PREFIX='/aws/vendedlogs/bedrock-agentcore/runtime/USAGE_LOGS'


def _agentcore_usage_delivery_names(runtime_id):
    suffix=hashlib.sha256(runtime_id.encode('utf-8')).hexdigest()[:16]
    return f'atsuite-usage-source-{suffix}',f'atsuite-usage-destination-{suffix}'


def _delete_agentcore_usage_logs(logs,runtime_arn,runtime_id):
    """Remove the per-runtime USAGE_LOGS delivery created at deploy time.

    Delivery resources are additive remote state, so cleanup must not depend on
    the runtime still existing. Names are deterministic from the runtime id; a
    live ARN lets us also catch sources created under a different name.
    """
    source_names=set();destination_names=set()
    if runtime_id:
        source,destination=_agentcore_usage_delivery_names(runtime_id)
        source_names.add(source);destination_names.add(destination)
    log_group=f'{AGENTCORE_USAGE_LOG_GROUP_PREFIX}/{runtime_id}' if runtime_id else ''
    def _paginate(operation,key):
        args={}
        while True:
            page=operation(**args)
            for row in page.get(key,[]):yield row
            token=page.get('nextToken')
            if not token:break
            args['nextToken']=token
    if runtime_arn:
        try:
            for source in _paginate(logs.describe_delivery_sources,'deliverySources'):
                if runtime_arn in (source.get('resourceArns') or []):source_names.add(source['name'])
        except Exception:pass
    if log_group:
        try:
            for destination in _paginate(logs.describe_delivery_destinations,'deliveryDestinations'):
                conf=destination.get('deliveryDestinationConfiguration') or {}
                if str(conf.get('destinationResourceArn','')).endswith(log_group):destination_names.add(destination['name'])
        except Exception:pass
    try:
        for delivery in _paginate(logs.describe_deliveries,'deliveries'):
            if delivery.get('deliverySourceName') in source_names:
                try:logs.delete_delivery(id=delivery['id'])
                except Exception:pass
    except Exception:pass
    for name in source_names:
        try:logs.delete_delivery_source(name=name)
        except Exception:pass
    for name in destination_names:
        try:logs.delete_delivery_destination(name=name)
        except Exception:pass
    if log_group:
        try:logs.delete_log_group(logGroupName=log_group)
        except Exception:pass


def cleanup_owned_resource(resource,record,*,clients=None,runner=subprocess.run):
    clients=clients or {}
    expected=digest({'tenant':record['tenant'],'project':record['project'],'experiment':record['id'],'candidate':resource['candidate_id']},length=64)
    if resource.get('ownership')!=expected or any(resource.get(k)!=record.get(k) for k in ('tenant','project')) or resource.get('experiment_id')!=record['id']:
        raise ValueError('cleanup_ownership_mismatch')
    provider=resource['provider'];region=resource['region'];account=resource['account_id'];name=resource['physical_name']
    if record.get('accounts',{}).get(provider)!=account or record['approval']['accounts'].get(provider,account)!=account:
        raise ValueError('cleanup_account_mismatch')
    if not name.startswith(('at-','atsuite_at_')): raise ValueError('cleanup_resource_not_experimental')
    if provider.startswith('aws_'):
        import boto3
        sts=clients.get('sts') or boto3.client('sts',region_name=region)
        if sts.get_caller_identity()['Account']!=account: raise ValueError('cleanup_actual_account_mismatch')
        if provider=='aws_lambda':
            client=clients.get('lambda') or boto3.client('lambda',region_name=region)
            try:
                value=client.get_function(FunctionName=name)
                arn=value['Configuration']['FunctionArn']
                if arn.split(':')[3:5]!=[region,account]: raise ValueError('cleanup_arn_mismatch')
                if client.list_tags(Resource=arn).get('Tags',{}).get('atsuite-ownership')!=expected: raise ValueError('cleanup_remote_ownership_mismatch')
                client.delete_function(FunctionName=arn)
                try:client.get_function(FunctionName=arn);return {'cleanup_status':'pending','readback':'deleting'}
                except client.exceptions.ResourceNotFoundException: return {'cleanup_status':'success','readback':'not_found'}
            except client.exceptions.ResourceNotFoundException: return {'cleanup_status':'success','readback':'not_found'}
        client=clients.get('agentcore') or boto3.client('bedrock-agentcore-control',region_name=region)
        logs=clients.get('logs') or boto3.client('logs',region_name=region)
        runtime_id=resource.get('runtime_id')
        if not runtime_id:
            args={}
            while True:
                page=client.list_agent_runtimes(**args)
                matches=[r for r in page.get('agentRuntimes',[]) if r.get('agentRuntimeName')==name]
                if matches: runtime_id=matches[0]['agentRuntimeId'];break
                if not page.get('nextToken'):return {'cleanup_status':'success','readback':'not_found'}
                args['nextToken']=page['nextToken']
        try:
            value=client.get_agent_runtime(agentRuntimeId=runtime_id);arn=value['agentRuntimeArn']
            if arn.split(':')[3:5]!=[region,account]:raise ValueError('cleanup_arn_mismatch')
            if client.list_tags_for_resource(resourceArn=arn).get('tags',{}).get('atsuite-ownership')!=expected:raise ValueError('cleanup_remote_ownership_mismatch')
            _delete_agentcore_usage_logs(logs,arn,runtime_id)
            client.delete_agent_runtime(agentRuntimeId=runtime_id)
            return {'cleanup_status':'pending','readback':'deleting','runtime_id':runtime_id}
        except client.exceptions.ResourceNotFoundException:
            _delete_agentcore_usage_logs(logs,None,runtime_id)
            return {'cleanup_status':'success','readback':'not_found'}
    if provider.startswith('gcp_'):
        response=runner(['gcloud','run','services','describe',name,'--project',account,'--region',region,'--format=json'],capture_output=True,text=True)
        if response.returncode:
            if 'NOT_FOUND' in response.stderr or 'not found' in response.stderr.lower():return {'cleanup_status':'success','readback':'not_found'}
            raise RuntimeError('cleanup_readback_failed')
        observed=json.loads(response.stdout)
        labels=observed.get('metadata',{}).get('labels',{})
        if labels.get('atsuite-ownership')!=expected[:63]:raise ValueError('cleanup_remote_ownership_mismatch')
        runner(['gcloud','run','services','delete',name,'--project',account,'--region',region,'--quiet'],check=True,capture_output=True,text=True)
        readback=runner(['gcloud','run','services','describe',name,'--project',account,'--region',region,'--format=json'],capture_output=True,text=True)
        if readback.returncode and ('NOT_FOUND' in readback.stderr or 'not found' in readback.stderr.lower()):return {'cleanup_status':'success','readback':'not_found'}
        return {'cleanup_status':'pending','readback':'unknown'}
    if provider.startswith('ali_'):
        from atsuite.ali.ali import Ali
        from alibabacloud_tea_util.models import RuntimeOptions
        from alibabacloud_fc20230330.models import GetFunctionRequest
        client=clients.get('fc') or Ali(region=region).get_fc_client()
        try:
            response=client.get_function_with_options(name,GetFunctionRequest(),{},RuntimeOptions());body=response.body
            tags={str(t.key):str(t.value) for t in (getattr(body,'tags',None) or [])}
            if tags.get('atsuite-ownership')!=expected:raise ValueError('cleanup_remote_ownership_mismatch')
            arn=str(getattr(body,'function_arn',''))
            if arn and (account not in arn or region not in arn):raise ValueError('cleanup_arn_mismatch')
            client.delete_function_with_options(name,{},RuntimeOptions())
            return {'cleanup_status':'pending','readback':'deleting'}
        except Exception as exc:
            if getattr(exc,'code','') in {'FunctionNotFound','ResourceNotFound','NotFound'}:return {'cleanup_status':'success','readback':'not_found'}
            raise
    raise ValueError('unsupported_owned_cleanup_provider')


def reconcile_expired(store,*,now=None,backend=None):
    import time
    from .cloud_backend import CloudBackend
    backend=backend or CloudBackend(); now=now or time.time();results=[]
    with store._lock:
        records=[json.loads(row[0]) for row in store.connection.execute('SELECT payload FROM experiments')]
    for record in records:
        resources=record.get('resource_records',{})
        expired=[k for k,v in resources.items() if v.get('expires_at') and v['expires_at']<=now and v.get('cleanup_status')!='success']
        if not expired:continue
        token=store.claim(record['id'],'ttl-reconciler',for_cleanup=True)
        if token is None: continue
        try:
            for key in expired:
                store.renew(record['id'],token)
                try:resources[key].update(backend.cleanup(resources[key],record))
                except Exception as exc:resources[key].update(cleanup_status='failed',cleanup_error=type(exc).__name__)
            store.checkpoint(record['id'],'CLEANUP_PENDING',{'resource_records':resources,'ttl_reconciled_at':now},lease_token=token)
            results.append({'experiment_id':record['id'],'resources':expired})
        finally:
            store.release(record['id'],token)
    return results
