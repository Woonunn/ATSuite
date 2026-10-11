from __future__ import annotations

import os
import unittest
from unittest import mock

from atsuite.aws.Agentcore import AWSAgentCore
from atsuite.execution.cleanup import cleanup_owned_resource
from atsuite.recommender.identity import digest


class FakeLogs:
    class exceptions:
        class ConflictException(Exception):
            pass

        class ResourceAlreadyExistsException(Exception):
            pass

        class ResourceNotFoundException(Exception):
            pass

    def __init__(self):
        self.groups = []
        self.group_tags = {}
        self.sources = {}
        self.destinations = {}
        self.deliveries = []
        self.deleted = []

    def create_log_group(self, logGroupName, tags=None):
        if logGroupName in self.groups:
            raise self.exceptions.ResourceAlreadyExistsException(logGroupName)
        self.groups.append(logGroupName)
        self.group_tags[logGroupName] = dict(tags or {})

    def put_delivery_source(self, name, resourceArn, logType, tags=None):
        self.sources[name] = {"name": name, "resourceArns": [resourceArn], "logType": logType, "tags":dict(tags or {})}
        return {"deliverySource": self.sources[name]}

    def put_delivery_destination(self, name, deliveryDestinationConfiguration, outputFormat, tags=None):
        self.destinations[name] = {"name": name, "deliveryDestinationConfiguration": deliveryDestinationConfiguration, "tags":dict(tags or {})}
        return {"deliveryDestination": self.destinations[name]}

    def create_delivery(self, deliverySourceName, deliveryDestinationArn, tags=None):
        if any(row["deliverySourceName"] == deliverySourceName and row['deliveryDestinationArn'] == deliveryDestinationArn for row in self.deliveries):
            raise self.exceptions.ConflictException(deliverySourceName)
        delivery={"id": "d"+str(len(self.deliveries)+1), "deliverySourceName": deliverySourceName, "deliveryDestinationArn": deliveryDestinationArn, "tags":dict(tags or {})}
        self.deliveries.append(delivery)
        return {'delivery':delivery}

    def get_delivery_source(self, name):
        if name not in self.sources:raise self.exceptions.ResourceNotFoundException(name)
        return {'deliverySource':{k:v for k,v in self.sources[name].items() if k!='tags'}}

    def get_delivery_destination(self, name):
        if name not in self.destinations:raise self.exceptions.ResourceNotFoundException(name)
        return {'deliveryDestination':{k:v for k,v in self.destinations[name].items() if k!='tags'}}

    def list_tags_for_resource(self, resourceArn):
        for kind,items in (('delivery-source',self.sources),('delivery-destination',self.destinations),
                           ('delivery',{d['id']:d for d in self.deliveries})):
            marker=':'+kind+':'
            if marker in resourceArn:
                name=resourceArn.split(marker,1)[1]
                if name not in items:raise self.exceptions.ResourceNotFoundException(name)
                return {'tags':items[name].get('tags',{})}
        name=resourceArn.split(':log-group:',1)[1]
        if name not in self.groups:raise self.exceptions.ResourceNotFoundException(name)
        return {'tags':self.group_tags[name]}

    def describe_log_groups(self, logGroupNamePrefix, **kwargs):
        return {'logGroups':[{'logGroupName':name} for name in self.groups if name.startswith(logGroupNamePrefix)]}

    def describe_delivery_sources(self, **kwargs):
        return {"deliverySources": list(self.sources.values())}

    def describe_deliveries(self, **kwargs):
        return {"deliveries": [{k:v for k,v in d.items() if k!='tags'} for d in self.deliveries]}

    def describe_delivery_destinations(self, **kwargs):
        return {"deliveryDestinations": list(self.destinations.values())}

    def delete_delivery(self, id):
        self.deliveries = [row for row in self.deliveries if row["id"] != id]
        self.deleted.append(("delivery", id))

    def delete_delivery_source(self, name):
        self.sources.pop(name, None)
        self.deleted.append(("source", name))

    def delete_delivery_destination(self, name):
        self.destinations.pop(name, None)
        self.deleted.append(("destination", name))

    def delete_log_group(self, logGroupName):
        self.groups = [group for group in self.groups if group != logGroupName]
        self.group_tags.pop(logGroupName,None)
        self.deleted.append(("log_group", logGroupName))


class FakeAgentCoreControl:
    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self, arn, ownership=""):
        self.arn = arn
        self.ownership = ownership
        self.deleted = []

    def get_agent_runtime(self, agentRuntimeId):
        if agentRuntimeId in self.deleted:raise self.exceptions.ResourceNotFoundException(agentRuntimeId)
        return {"agentRuntimeArn": self.arn}

    def list_tags_for_resource(self, resourceArn):
        return {"tags": {"atsuite-ownership": self.ownership}}

    def delete_agent_runtime(self, agentRuntimeId):
        self.deleted.append(agentRuntimeId)


class FakeSts:
    def __init__(self, account):
        self.account = account

    def get_caller_identity(self):
        return {"Account": self.account}


class AgentCoreUsageLogDeliveryTests(unittest.TestCase):
    def make_deployer(self, logs):
        node_name = "at-" + "a" * 40
        control = FakeAgentCoreControl(
            "arn:aws:bedrock-agentcore:us-east-1:111:runtime/" + "atsuite_" + node_name.replace("-", "_")
        )
        with mock.patch.dict(os.environ, {"AWS_ACCOUNT_ID": "111", "ECR_REPOSITORY_NAME": "repo"}):
            deployer = AWSAgentCore(
                node_name=node_name,
                tag="tag",
                ecr_client=None,
                agentcore_client=control,
                region="us-east-1",
                runtime_options={"strict_experiment": True, "ownership": "own"},
                logs_client=logs,
            )
        deployer._runtime_id = deployer.runtime_name
        return deployer

    def test_setup_creates_log_group_delivery_source_destination_and_delivery(self):
        logs = FakeLogs()
        deployer = self.make_deployer(logs)
        deployer._setup_usage_logs()
        expected_group = AWSAgentCore.usage_log_group(deployer._runtime_id)
        self.assertEqual(logs.groups, [expected_group])
        self.assertEqual([source["logType"] for source in logs.sources.values()], ["USAGE_LOGS"])
        self.assertEqual(len(logs.destinations), 1)
        self.assertEqual(len(logs.deliveries), 1)
        for item in [*logs.sources.values(),*logs.destinations.values(),*logs.deliveries]:
            self.assertEqual(item['tags']['atsuite-ownership'],'own')
        self.assertEqual(logs.group_tags[expected_group]['atsuite-ownership'],'own')

    def test_setup_is_idempotent_on_redeploy(self):
        logs = FakeLogs()
        deployer = self.make_deployer(logs)
        deployer._setup_usage_logs()
        deployer._setup_usage_logs()
        self.assertEqual(len(logs.groups), 1)
        self.assertEqual(len(logs.sources), 1)
        self.assertEqual(len(logs.destinations), 1)
        self.assertEqual(len(logs.deliveries), 1)

    def test_cleanup_removes_delivery_resources_and_log_group(self):
        account, region = "111", "us-east-1"
        runtime_id = "atsuite_at_" + "a" * 40
        arn = f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtime_id}"
        logs = FakeLogs()
        ownership = digest({"tenant": "t", "project": "p", "experiment": "exp-1", "candidate": "c1"}, length=64)
        tags={'atsuite-ownership':ownership}
        source_name, destination_name = AWSAgentCore.usage_delivery_names(runtime_id)
        logs.put_delivery_source(source_name, arn, "USAGE_LOGS",tags=tags)
        logs.put_delivery_destination(
            destination_name,
            {"destinationResourceArn": f"arn:aws:logs:{region}:{account}:log-group:{AWSAgentCore.usage_log_group(runtime_id)}"},
            "json",tags=tags,
        )
        logs.create_delivery(source_name, f"arn:aws:logs:{region}:{account}:delivery-destination:{destination_name}",tags=tags)
        logs.create_log_group(AWSAgentCore.usage_log_group(runtime_id),tags=tags)
        control = FakeAgentCoreControl(arn, ownership=ownership)
        record = {"id": "exp-1", "tenant": "t", "project": "p", "accounts": {"aws_agentcore": account},
                  "approval": {"accounts": {"aws_agentcore": account}}}
        resource = {"provider": "aws_agentcore", "region": region, "account_id": account, "physical_name": runtime_id,
                    "ownership": ownership, "tenant": "t", "project": "p", "experiment_id": "exp-1",
                    "candidate_id": "c1", "runtime_id": runtime_id}

        result = cleanup_owned_resource(resource, record, clients={"sts": FakeSts(account), "agentcore": control, "logs": logs})

        self.assertEqual(result["cleanup_status"], "pending")
        self.assertEqual(control.deleted, [runtime_id])
        self.assertEqual(logs.sources, {})
        self.assertEqual(logs.destinations, {})
        self.assertEqual(logs.deliveries, [])
        self.assertEqual(logs.groups, [])


class AgentCoreUsageLogRecoveryTests(unittest.TestCase):
    make_deployer = AgentCoreUsageLogDeliveryTests.make_deployer

    def make_case(self, *, gone=False):
        account,region='111','us-east-1'
        name='atsuite_at_'+'a'*37
        runtime_id=name+'-abc123'
        arn=f'arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtime_id}'
        ownership=digest({'tenant':'t','project':'p','experiment':'e','candidate':'c'},length=64)
        tags={'atsuite-ownership':ownership}
        logs=FakeLogs()
        source,destination=AWSAgentCore.usage_delivery_names(runtime_id)
        group=AWSAgentCore.usage_log_group(runtime_id)
        logs.create_log_group(group,tags=tags)
        logs.put_delivery_source(source,arn,'USAGE_LOGS',tags=tags)
        logs.put_delivery_destination(destination,{'destinationResourceArn':f'arn:aws:logs:{region}:{account}:log-group:{group}'},'json',tags=tags)
        logs.create_delivery(source,f'arn:aws:logs:{region}:{account}:delivery-destination:{destination}',tags=tags)
        control=FakeAgentCoreControl(arn,ownership)
        if gone:control.deleted.append(runtime_id)
        control.list_agent_runtimes=mock.Mock(return_value={'agentRuntimes':[] if gone else [
            {'agentRuntimeName':name,'agentRuntimeId':runtime_id}]})
        record={'id':'e','tenant':'t','project':'p','accounts':{'aws_agentcore':account},'approval':{'accounts':{'aws_agentcore':account}}}
        resource={'provider':'aws_agentcore','region':region,'account_id':account,'physical_name':name,
                  'runtime_id':runtime_id,'ownership':ownership,'tenant':'t','project':'p','experiment_id':'e','candidate_id':'c'}
        return logs,control,record,resource

    def cleanup(self, logs, control, record, resource):
        return cleanup_owned_resource(resource,record,clients={
            'logs':logs,'agentcore':control,'sts':FakeSts(resource['account_id'])})

    def test_deletion_and_discovery_failures_are_retryable_not_success(self):
        from botocore.exceptions import ClientError
        operations=['get_delivery_source','get_delivery_destination','list_tags_for_resource',
                    'describe_deliveries','describe_delivery_destinations','delete_delivery',
                    'delete_delivery_source','delete_delivery_destination','delete_log_group']
        for operation in operations:
            for code in ('AccessDeniedException','ThrottlingException','ServiceUnavailableException'):
                with self.subTest(operation=operation,code=code):
                    logs,control,record,resource=self.make_case(gone=True)
                    error=ClientError({'Error':{'Code':code,'Message':'simulated failure'}},operation)
                    with mock.patch.object(logs,operation,side_effect=error), self.assertRaises(ClientError):
                        self.cleanup(logs,control,record,resource)
                    result=self.cleanup(logs,control,record,resource)
                    self.assertEqual(result['cleanup_status'],'success')
                    self.assertFalse(logs.groups or logs.sources or logs.destinations or logs.deliveries)

    def test_runtime_already_deleting_remains_pending_without_second_delete(self):
        logs,control,record,resource=self.make_case()
        control.get_agent_runtime=mock.Mock(return_value={'agentRuntimeArn':control.arn,'status':'DELETING'})
        result=self.cleanup(logs,control,record,resource)
        self.assertEqual(result['cleanup_status'],'pending')
        self.assertEqual(control.deleted,[])
        self.assertEqual(logs.deleted,[])

    def test_unrelated_application_trace_and_usage_routes_are_preserved(self):
        logs,control,record,resource=self.make_case()
        for log_type in ('APPLICATION_LOGS','TRACES','USAGE_LOGS'):
            source='external-'+log_type
            logs.put_delivery_source(source,control.arn,log_type)
            logs.create_delivery(source,'external-destination')
        result=self.cleanup(logs,control,record,resource)
        self.assertEqual(result['cleanup_status'],'pending')
        self.assertEqual(set(logs.sources),{'external-'+t for t in ('APPLICATION_LOGS','TRACES','USAGE_LOGS')})
        self.assertEqual(len(logs.deliveries),3)
        self.assertTrue(all(d['deliverySourceName'].startswith('external-') for d in logs.deliveries))

    def test_wrong_or_missing_ownership_refuses_all_deletions(self):
        for kind in ('group','source','destination','delivery'):
            for tags in ({},{'atsuite-ownership':'foreign'}):
                with self.subTest(kind=kind,tags=tags):
                    logs,control,record,resource=self.make_case()
                    if kind == 'group':logs.group_tags[logs.groups[0]]=tags
                    elif kind == 'source':next(iter(logs.sources.values()))['tags']=tags
                    elif kind == 'destination':next(iter(logs.destinations.values()))['tags']=tags
                    else:logs.deliveries[0]['tags']=tags
                    with self.assertRaisesRegex(ValueError,'cleanup_logs_'):
                        self.cleanup(logs,control,record,resource)
                    self.assertEqual(logs.deleted,[])
                    self.assertEqual(control.deleted,[])

    def test_wrong_log_type_and_shared_routes_refuse_deletion(self):
        for scenario in ('wrong_type','shared_source','shared_destination','shared_group'):
            with self.subTest(scenario=scenario):
                logs,control,record,resource=self.make_case()
                source=next(iter(logs.sources))
                destination=next(iter(logs.destinations.values()))
                if scenario == 'wrong_type':logs.sources[source]['logType']='APPLICATION_LOGS'
                elif scenario == 'shared_source':logs.create_delivery(source,'external-destination')
                elif scenario == 'shared_destination':logs.create_delivery('external-source',logs.deliveries[0]['deliveryDestinationArn'])
                else:logs.put_delivery_destination('external-destination',destination['deliveryDestinationConfiguration'],'json')
                with self.assertRaisesRegex(ValueError,'cleanup_logs_'):
                    self.cleanup(logs,control,record,resource)
                self.assertEqual(logs.deleted,[])
                self.assertEqual(control.deleted,[])

    def test_deleted_runtime_and_incomplete_journal_recover_partial_logs(self):
        for remaining in ('all','group','source','destination'):
            with self.subTest(remaining=remaining):
                logs,control,record,resource=self.make_case(gone=True)
                resource.pop('runtime_id')
                if remaining != 'all':
                    logs.deliveries=[]
                    if remaining != 'group':logs.groups=[]
                    if remaining != 'source':logs.sources={}
                    if remaining != 'destination':logs.destinations={}
                result=self.cleanup(logs,control,record,resource)
                self.assertEqual(result['cleanup_status'],'success')
                self.assertFalse(logs.groups or logs.sources or logs.destinations or logs.deliveries)
                self.assertTrue(logs.deleted)

    def test_orphan_discovery_follows_pagination_and_keeps_other_runtimes(self):
        logs,control,record,resource=self.make_case(gone=True)
        resource.pop('runtime_id')
        sources=list(logs.sources.values())
        unrelated={'name':'external-source','resourceArns':[control.arn+'-other'],'logType':'APPLICATION_LOGS'}
        logs.sources[unrelated['name']]=unrelated
        with mock.patch.object(logs,'describe_delivery_sources',side_effect=[
            {'deliverySources':[unrelated],'nextToken':'page2'}, {'deliverySources':sources}]) as pages:
            result=self.cleanup(logs,control,record,resource)
        self.assertEqual(result['cleanup_status'],'success')
        self.assertEqual(pages.call_args_list,[mock.call(),mock.call(nextToken='page2')])
        self.assertEqual(logs.sources,{'external-source':unrelated})

    def test_cleaned_runtime_and_logs_can_be_reconciled_again(self):
        logs,control,record,resource=self.make_case()
        self.assertEqual(self.cleanup(logs,control,record,resource)['cleanup_status'],'pending')
        self.assertEqual(self.cleanup(logs,control,record,resource)['cleanup_status'],'success')
        resource.pop('runtime_id')
        self.assertEqual(self.cleanup(logs,control,record,resource)['cleanup_status'],'success')

    def test_not_found_during_log_deletion_is_idempotent(self):
        from botocore.exceptions import ClientError
        logs,control,record,resource=self.make_case(gone=True)
        delete=logs.delete_delivery
        def raced_delete(**kwargs):
            delete(**kwargs)
            raise ClientError({'Error':{'Code':'ResourceNotFoundException'}},'DeleteDelivery')
        with mock.patch.object(logs,'delete_delivery',side_effect=raced_delete):
            self.assertEqual(self.cleanup(logs,control,record,resource)['cleanup_status'],'success')
        self.assertFalse(logs.groups or logs.sources or logs.destinations or logs.deliveries)

    def test_setup_never_overwrites_foreign_log_resources(self):
        for kind in ('group','source','destination'):
            with self.subTest(kind=kind):
                logs=FakeLogs();deployer=self.make_deployer(logs)
                deployer._setup_usage_logs()
                if kind == 'group':logs.group_tags[logs.groups[0]]={}
                elif kind == 'source':next(iter(logs.sources.values()))['tags']={}
                else:next(iter(logs.destinations.values()))['tags']={}
                with self.assertRaisesRegex(RuntimeError,'different ownership'):
                    deployer._setup_usage_logs()
                self.assertEqual(len(logs.deliveries),1)

    def test_runtime_checkpoint_precedes_endpoint_and_log_creation(self):
        logs=FakeLogs();deployer=self.make_deployer(logs)
        events=[]
        deployer.runtime_options['on_resource']=events.append
        deployer.image_ref='repo@sha256:fixture'
        with mock.patch.object(deployer,'_create_or_update_runtime'), mock.patch.object(
            deployer,'_ensure_endpoint',side_effect=RuntimeError('endpoint failure')):
            with self.assertRaisesRegex(RuntimeError,'endpoint failure'):deployer.deploy()
        self.assertEqual(events[0]['runtime_id'],deployer._runtime_id)
        self.assertEqual(events[0]['physical_name'],deployer.runtime_name)
        self.assertEqual(len(events[0]['physical_name']),48)
        self.assertEqual(logs.groups,[])

    def test_log_intent_is_checkpointed_before_partial_setup_failure(self):
        logs=FakeLogs();deployer=self.make_deployer(logs)
        events=[];deployer.runtime_options['on_resource']=events.append
        with mock.patch.object(logs,'put_delivery_destination',side_effect=RuntimeError('delivery setup failure')):
            with self.assertRaisesRegex(RuntimeError,'delivery setup failure'):deployer._setup_usage_logs()
        self.assertEqual(events[0]['runtime_id'],deployer._runtime_id)
        self.assertEqual(events[0]['usage_logs']['log_group'],logs.groups[0])
        self.assertEqual(events[0]['usage_logs']['source_name'],next(iter(logs.sources)))

    def test_deploy_orchestration_writes_early_runtime_checkpoint(self):
        from pathlib import Path
        from types import SimpleNamespace
        from atsuite.deploy import deploy

        runtime=SimpleNamespace(architecture='linux/arm64',concurrency=1,billing_mode='on_demand',to_dict=lambda:{})
        target=SimpleNamespace(name='tool',family='mcp',runtime=runtime)
        resolved=SimpleNamespace(family='mcp',bench_name='benchmark',targets={'tool':target})
        journal=[]
        deployer=mock.Mock()
        def failed_deploy(name, runtime, *, image_ref, runtime_options):
            runtime_options['on_resource']({'physical_name':('atsuite_'+name.replace('-','_'))[:48],
                                           'runtime_id':'runtime-saved-before-failure'})
            raise RuntimeError('interrupted deploy')
        deployer.deploy_target.side_effect=failed_deploy
        with (mock.patch('atsuite.deploy.is_sandbox_config',return_value=False),
              mock.patch('atsuite.deploy.resolve_benchmark',return_value=resolved),
              mock.patch('atsuite.deploy.logical_state_domain',return_value='state'),
              mock.patch('atsuite.mcp.mcp.AWSAgentCoreMCPDeployer',return_value=deployer)):
            with self.assertRaisesRegex(RuntimeError,'interrupted deploy'):
                deploy(Path('unused.json'),'aws_agentcore',experiment_id='e',candidate_id='c',
                       region='us-east-1',ownership='own',on_resource=journal.append)
        self.assertEqual(len(journal[0]['physical_name']),48)
        self.assertEqual(journal[-1]['runtime_id'],'runtime-saved-before-failure')
        self.assertEqual(journal[-1]['logical_target'],'tool')
        self.assertEqual(journal[-1]['physical_name'],journal[0]['physical_name'])

    def test_failed_final_readback_preserves_early_runtime_identity(self):
        from pathlib import Path
        from types import SimpleNamespace
        from atsuite.deploy import deploy

        runtime=SimpleNamespace(architecture='linux/arm64',concurrency=1,billing_mode='on_demand',to_dict=lambda:{})
        target=SimpleNamespace(name='tool',family='mcp',runtime=runtime)
        resolved=SimpleNamespace(family='mcp',bench_name='benchmark',targets={'tool':target},nodes={})
        journal={}
        deployer=mock.Mock()
        def deployed(name,runtime,*,image_ref,runtime_options):
            runtime_options['on_resource']({'physical_name':('atsuite_'+name.replace('-','_'))[:48],
                                           'runtime_id':'saved-runtime-id','usage_logs':{'source_name':'saved-source'}})
            return 'https://endpoint'
        deployer.deploy_target.side_effect=deployed
        deployer.readback_target.side_effect=RuntimeError('readback failed')
        with (mock.patch('atsuite.deploy.is_sandbox_config',return_value=False),
              mock.patch('atsuite.deploy.resolve_benchmark',return_value=resolved),
              mock.patch('atsuite.deploy.logical_state_domain',return_value='state'),
              mock.patch('atsuite.deploy._allowed_tools_for_target',return_value=[]),
              mock.patch('atsuite.deploy._endpoint_entry',return_value={}),
              mock.patch('atsuite.deploy._write_map'),
              mock.patch('atsuite.mcp.mcp.AWSAgentCoreMCPDeployer',return_value=deployer)):
            deploy(Path('unused.json'),'aws_agentcore',experiment_id='e',candidate_id='c',region='us-east-1',
                   ownership='own',on_resource=journal.update)
        self.assertEqual(journal['runtime_id'],'saved-runtime-id')
        self.assertEqual(journal['usage_logs']['source_name'],'saved-source')
        self.assertEqual(len(journal['physical_name']),48)
        self.assertEqual(journal['status'],'created')

    def test_setup_requests_match_real_sdk_shapes(self):
        import boto3
        from botocore.stub import Stubber

        logs=boto3.client('logs',region_name='us-east-1',aws_access_key_id='fixture',aws_secret_access_key='fixture')
        deployer=self.make_deployer(logs)
        group=AWSAgentCore.usage_log_group(deployer._runtime_id)
        source,destination=AWSAgentCore.usage_delivery_names(deployer._runtime_id)
        group_arn=f'arn:aws:logs:us-east-1:111:log-group:{group}'
        destination_arn=f'arn:aws:logs:us-east-1:111:delivery-destination:{destination}'
        tags={'atsuite-ownership':'own'}
        with Stubber(logs) as stub:
            stub.add_client_error('list_tags_for_resource',service_error_code='ResourceNotFoundException',expected_params={'resourceArn':group_arn})
            stub.add_client_error('get_delivery_source',service_error_code='ResourceNotFoundException',expected_params={'name':source})
            stub.add_client_error('get_delivery_destination',service_error_code='ResourceNotFoundException',expected_params={'name':destination})
            stub.add_response('create_log_group',{},expected_params={'logGroupName':group,'tags':tags})
            stub.add_response('put_delivery_source',{},expected_params={'name':source,'resourceArn':deployer.agentcore_client.arn,'logType':'USAGE_LOGS','tags':tags})
            stub.add_response('put_delivery_destination',{},expected_params={'name':destination,'deliveryDestinationConfiguration':{'destinationResourceArn':group_arn},'outputFormat':'json','tags':tags})
            stub.add_response('create_delivery',{'delivery':{'id':'d123'}},expected_params={'deliverySourceName':source,'deliveryDestinationArn':destination_arn,'tags':tags})
            deployer._setup_usage_logs()
            stub.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main()
