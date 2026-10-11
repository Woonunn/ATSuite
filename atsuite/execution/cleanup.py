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


def _logs_if_present(action):
    try:
        return action()
    except Exception as exc:
        code=getattr(exc,'response',{}).get('Error',{}).get('Code','')
        if code == 'ResourceNotFoundException' or type(exc).__name__ == 'ResourceNotFoundException':
            return None
        raise


def _logs_pages(operation,key,**args):
    while True:
        page=operation(**args)
        yield from page.get(key,[])
        token=page.get('nextToken')
        if not token:return
        args['nextToken']=token


def _delete_agentcore_usage_logs(logs,runtime_arn,runtime_id,*,ownership):
    """Validate all child resources before deleting this runtime's owned usage route."""
    source_name,destination_name=_agentcore_usage_delivery_names(runtime_id)
    log_group=f'{AGENTCORE_USAGE_LOG_GROUP_PREFIX}/{runtime_id}'
    arn_prefix=':'.join(runtime_arn.split(':')[:5]).replace(':bedrock-agentcore:',':logs:')
    group_arn=f'{arn_prefix}:log-group:{log_group}'
    destination_arn=f'{arn_prefix}:delivery-destination:{destination_name}'
    source_arn=f'{arn_prefix}:delivery-source:{source_name}'
    source=_logs_if_present(lambda:logs.get_delivery_source(name=source_name))
    destination=_logs_if_present(lambda:logs.get_delivery_destination(name=destination_name))
    group=_logs_if_present(lambda:logs.list_tags_for_resource(resourceArn=group_arn))
    source=source['deliverySource'] if source is not None else None
    destination=destination['deliveryDestination'] if destination is not None else None
    # Get/Describe delivery APIs omit tags; use the dedicated tagging API.
    for item,arn in ((source,source_arn),(destination,destination_arn),(group,group_arn)):
        if item is None:continue
        tags=group if arn == group_arn else _logs_if_present(lambda:logs.list_tags_for_resource(resourceArn=arn))
        if (tags or {}).get('tags',{}).get('atsuite-ownership') != ownership:
            raise ValueError('cleanup_logs_ownership_mismatch')
    if source is not None and (source.get('logType') != 'USAGE_LOGS' or source.get('resourceArns') != [runtime_arn]):
        raise ValueError('cleanup_logs_source_mismatch')
    if destination is not None and destination.get('deliveryDestinationConfiguration',{}).get('destinationResourceArn') != group_arn:
        raise ValueError('cleanup_logs_destination_mismatch')
    deliveries=[]
    for delivery in _logs_pages(logs.describe_deliveries,'deliveries'):
        if delivery.get('deliverySourceName') != source_name and delivery.get('deliveryDestinationArn') != destination_arn:
            continue
        if delivery.get('deliverySourceName') != source_name or delivery.get('deliveryDestinationArn') != destination_arn:
            raise ValueError('cleanup_logs_shared_delivery')
        tags=logs.list_tags_for_resource(resourceArn=f"{arn_prefix}:delivery:{delivery['id']}")
        if tags.get('tags',{}).get('atsuite-ownership') != ownership:
            raise ValueError('cleanup_logs_shared_delivery')
        deliveries.append(delivery)
    for item in _logs_pages(logs.describe_delivery_destinations,'deliveryDestinations'):
        if item['name'] != destination_name and item.get('deliveryDestinationConfiguration',{}).get('destinationResourceArn') == group_arn:
            raise ValueError('cleanup_logs_shared_log_group')
    for delivery in deliveries:
        _logs_if_present(lambda:logs.delete_delivery(id=delivery['id']))
    if source is not None:
        _logs_if_present(lambda:logs.delete_delivery_source(name=source_name))
    if destination is not None:
        _logs_if_present(lambda:logs.delete_delivery_destination(name=destination_name))
    if group is not None:
        _logs_if_present(lambda:logs.delete_log_group(logGroupName=log_group))


def _orphan_agentcore_runtime_ids(logs,name,region,account):
    """Recover older interrupted journals using only the exact experimental runtime name."""
    name=name[:48]
    ids=set()
    def add(runtime_id):
        if runtime_id == name or runtime_id.startswith(name+'-'):ids.add(runtime_id)
    runtime_prefix=f'arn:aws:bedrock-agentcore:{region}:{account}:runtime/'
    group_prefix=f'arn:aws:logs:{region}:{account}:log-group:{AGENTCORE_USAGE_LOG_GROUP_PREFIX}/'
    for source in _logs_pages(logs.describe_delivery_sources,'deliverySources'):
        if source.get('logType') != 'USAGE_LOGS':continue
        for arn in source.get('resourceArns',[]):
            if arn.startswith(runtime_prefix):
                runtime_id=arn[len(runtime_prefix):]
                if source['name'] == _agentcore_usage_delivery_names(runtime_id)[0]:add(runtime_id)
    for destination in _logs_pages(logs.describe_delivery_destinations,'deliveryDestinations'):
        arn=destination.get('deliveryDestinationConfiguration',{}).get('destinationResourceArn','')
        if arn.startswith(group_prefix):
            runtime_id=arn[len(group_prefix):]
            if destination['name'] == _agentcore_usage_delivery_names(runtime_id)[1]:add(runtime_id)
    prefix=f'{AGENTCORE_USAGE_LOG_GROUP_PREFIX}/{name}'
    for group in _logs_pages(logs.describe_log_groups,'logGroups',logGroupNamePrefix=prefix):
        add(group['logGroupName'].removeprefix(AGENTCORE_USAGE_LOG_GROUP_PREFIX+'/'))
    return sorted(ids)


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
        name=name[:48]
        if not runtime_id:
            args={}
            while True:
                page=client.list_agent_runtimes(**args)
                matches=[r for r in page.get('agentRuntimes',[]) if r.get('agentRuntimeName')==name]
                if matches: runtime_id=matches[0]['agentRuntimeId'];break
                if not page.get('nextToken'):
                    for orphan in _orphan_agentcore_runtime_ids(logs,name,region,account):
                        arn=f'arn:aws:bedrock-agentcore:{region}:{account}:runtime/{orphan}'
                        _delete_agentcore_usage_logs(logs,arn,orphan,ownership=expected)
                    return {'cleanup_status':'success','readback':'not_found'}
                args['nextToken']=page['nextToken']
        try:
            value=client.get_agent_runtime(agentRuntimeId=runtime_id);arn=value['agentRuntimeArn']
        except client.exceptions.ResourceNotFoundException:
            arn=f'arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtime_id}'
            _delete_agentcore_usage_logs(logs,arn,runtime_id,ownership=expected)
            return {'cleanup_status':'success','readback':'not_found','runtime_id':runtime_id}
        if arn.split(':')[3:5]!=[region,account]:raise ValueError('cleanup_arn_mismatch')
        if client.list_tags_for_resource(resourceArn=arn).get('tags',{}).get('atsuite-ownership')!=expected:raise ValueError('cleanup_remote_ownership_mismatch')
        if value.get('status') == 'DELETING':
            return {'cleanup_status':'pending','readback':'deleting','runtime_id':runtime_id}
        _delete_agentcore_usage_logs(logs,arn,runtime_id,ownership=expected)
        try:
            client.delete_agent_runtime(agentRuntimeId=runtime_id)
        except client.exceptions.ResourceNotFoundException:
            return {'cleanup_status':'success','readback':'not_found','runtime_id':runtime_id}
        return {'cleanup_status':'pending','readback':'deleting','runtime_id':runtime_id}
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
        from alibabacloud_fc20230330.models import GetFunctionRequest,ListTriggersRequest
        client=clients.get('fc') or Ali(region=region).get_fc_client()
        try:
            response=client.get_function_with_options(name,GetFunctionRequest(),{},RuntimeOptions());body=response.body
            tags={str(t.key):str(t.value) for t in (getattr(body,'tags',None) or [])}
            if tags.get('atsuite-ownership')!=expected:raise ValueError('cleanup_remote_ownership_mismatch')
            arn=str(getattr(body,'function_arn',''))
            if arn and (account not in arn or region not in arn):raise ValueError('cleanup_arn_mismatch')
            next_token=None
            triggers=[]
            while True:
                page=client.list_triggers_with_options(name,ListTriggersRequest(next_token=next_token),{},RuntimeOptions()).body
                triggers.extend(t.trigger_name for t in (page.triggers or []))
                next_token=page.next_token
                if not next_token:break
            for trigger in triggers:
                client.delete_trigger_with_options(name,trigger,{},RuntimeOptions())
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
