import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from atsuite.aws.Lambda import AWSLambda
from atsuite.aws.Agentcore import AWSAgentCore
from atsuite.gcp.fc import GCPFC
from atsuite.faas.function import AWSFunctionDeployer, _cloud_run_readback, _cloud_run_authorization


class ProviderOptionsTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {'AWS_ACCOUNT_ID': '123', 'ECR_REPOSITORY_NAME': 'repo'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_lambda_strict_does_not_mutate_customer_function_or_role(self):
        client = mock.Mock()
        client.exceptions.ResourceNotFoundException = type('NotFound', (Exception,), {})
        client.get_function.return_value = {'Configuration': {'FunctionArn': 'arn:f', 'MemorySize': 512},
                                             'Code': {'ResolvedImageUri': 'repo@sha256:wrong'}}
        client.list_tags.return_value = {'Tags': {'atsuite-ownership': 'different'}}
        iam = mock.Mock()
        iam.list_attached_role_policies.return_value = {'AttachedPolicies': []}
        fn = AWSLambda('existing', [], 'tag', client, mock.Mock(), 'us-east-1', iam_client=iam,
                       runtime_options={'strict_experiment': True, 'ownership': 'owner'})
        with self.assertRaises(RuntimeError):
            fn.create_or_update_function('repo@sha256:expected')
        client.update_function_code.assert_not_called()
        iam.attach_role_policy.assert_not_called()

    def test_lambda_creation_propagates_architecture_identity_tags(self):
        client = mock.Mock()
        fn = AWSLambda('new', [], 'tag', client, mock.Mock(), 'us-east-2',
            runtime_options={'strict_experiment': True, 'ownership': 'owner', 'architecture': 'linux/arm64',
                             'environment': {'ATSUITE_TENANT': 'tenant', 'ATSUITE_CANDIDATE_ID': 'candidate'}})
        fn.create_function('repo@sha256:expected')
        request = client.create_function.call_args.kwargs
        self.assertEqual(request['Architectures'], ['arm64'])
        self.assertEqual(request['Environment']['Variables']['ATSUITE_CANDIDATE_ID'], 'candidate')
        self.assertEqual(request['Tags']['atsuite-ownership'], 'owner')

    def test_gcp_fractional_cpu_options_and_no_silent_timeout_clamp(self):
        options = {'concurrency': 1, 'billing_mode': 'request_based', 'execution_environment': 'gen1',
                   'environment': {'ATSUITE_TENANT': 't', 'ATSUITE_CANDIDATE_ID': 'c'}}
        fn = GCPFC('service', [], 'tag', 'project', 'asia-east1', cpu=.5, memory_size=512,
                   image_ref='repo@sha256:expected', runtime_options=options)
        with mock.patch('atsuite.gcp.fc.run') as run, mock.patch.object(fn, '_get_service_url', return_value='http://endpoint'):
            fn.deploy()
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('--concurrency')+1], '1')
        self.assertEqual(command[command.index('--region')+1], 'asia-east1')
        self.assertIn('--execution-environment', command)
        self.assertIn('--cpu-throttling', command)
        self.assertIn('ATSUITE_CANDIDATE_ID=c', command[command.index('--set-env-vars')+1])
        with self.assertRaises(ValueError):
            GCPFC('s', [], 'tag', 'p', 'r', timeout=4000, runtime_options={'strict_experiment': True, 'ownership': 'owner'})

    def test_agentcore_strict_conflict_never_updates_foreign_runtime(self):
        client = mock.Mock()
        client.exceptions.ConflictException = type('Conflict', (Exception,), {})
        client.create_agent_runtime.side_effect = client.exceptions.ConflictException()
        client.list_agent_runtimes.return_value = {'agentRuntimes': [{'agentRuntimeName': 'atsuite_service', 'agentRuntimeId': 'runtime'}]}
        client.get_agent_runtime.return_value = {'agentRuntimeArn': 'arn:r', 'agentRuntimeArtifact': {'containerConfiguration': {'containerUri': 'other'}}}
        client.list_tags_for_resource.return_value = {'tags': {'atsuite-ownership': 'other'}}
        runtime = AWSAgentCore('service', 'tag', mock.Mock(), client, 'us-east-1',
            runtime_options={'strict_experiment': True, 'ownership': 'owner'})
        with self.assertRaises(RuntimeError):
            runtime._create_or_update_runtime('repo@sha256:expected')
        client.update_agent_runtime.assert_not_called()

    def test_lambda_readback_derives_cpu_from_observation_and_keeps_actual_digest(self):
        deployer = object.__new__(AWSFunctionDeployer)
        client = mock.Mock()
        client.get_function.return_value = {'Configuration': {'FunctionArn': 'arn:aws:lambda:us-east-2:123:function:owned',
            'MemorySize': 1536, 'Timeout': 40, 'EphemeralStorage': {'Size': 700}, 'Architectures': ['arm64'], 'State': 'Active'},
            'Code': {'ResolvedImageUri': 'repo@sha256:observed'}}
        client.list_tags.return_value = {'Tags': {'atsuite-ownership': 'owner'}}
        deployer._targets = {'owned': SimpleNamespace(lambda_client=client, function_name='owned')}
        result = deployer.readback_target('owned')
        self.assertEqual(result['effective_resources']['cpu'], round(1536/1769, 3))
        self.assertEqual(result['field_semantics']['cpu'], 'official_rule_derived')
        self.assertEqual(result['image_digest'], 'sha256:observed')
        self.assertEqual(result['effective_region'], 'us-east-2')

    def test_cloud_run_readback_normalizes_units_and_does_not_impute_unknown(self):
        fn = SimpleNamespace(function_name='owned', region='requested', project_id='project')
        observed = {'metadata': {'labels': {'cloud.googleapis.com/location': 'observed', 'atsuite-ownership': 'owner'}},
            'spec': {'template': {'metadata': {'annotations': {'run.googleapis.com/cpu-throttling': 'false'}},
                'spec': {'containers': [{'image': 'repo@sha256:observed', 'resources': {'limits': {'cpu': '500m', 'memory': '2Gi'}}}],
                    'containerConcurrency': 1}}}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        with mock.patch('subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(observed))):
            result = _cloud_run_readback(fn)
        self.assertEqual(result['effective_resources']['cpu'], .5)
        self.assertEqual(result['effective_resources']['memory'], 2048)
        self.assertNotIn('timeout', result['effective_resources'])
        self.assertEqual(result['effective_region'], 'observed')
        self.assertEqual(result['effective_resources']['billing_mode'], 'instance_based')

    def test_gcp_strict_conflict_refuses_existing_unowned_resource(self):
        fn = GCPFC('existing', [], 'tag', 'project', 'us-central1', image_ref='repo@sha256:expected',
                   runtime_options={'strict_experiment': True, 'ownership': 'owner'})
        existing = {'metadata': {'labels': {'atsuite-ownership': 'foreign'}},
                    'spec': {'template': {'spec': {'containers': [{'image': 'repo@sha256:expected'}]}}}}
        with (mock.patch('subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(existing))),
              mock.patch('atsuite.gcp.fc.run') as mutation):
            with self.assertRaises(RuntimeError):
                fn.deploy()
        mutation.assert_not_called()

    def test_ali_creation_identity_and_failures_are_real_failures(self):
        from atsuite.ali.fc import AliFC
        client = mock.Mock()
        notfound = type('NotFound', (Exception,), {'code': 'FunctionNotFound'})
        client.get_function_with_options.side_effect = notfound()
        options = {'strict_experiment': True, 'ownership': 'owner', 'concurrency': 1,
                   'environment': {'ATSUITE_CANDIDATE_ID': 'candidate'}}
        fn = AliFC('owned', [], 'tag', client, runtime_options=options)
        fn.create_function('repo@sha256:expected')
        body = client.create_function_with_options.call_args.args[0].body.to_map()
        self.assertEqual(body['instanceConcurrency'], 1)
        self.assertEqual(body['tags'], [{'Key': 'atsuite-ownership', 'Value': 'owner'}])
        self.assertNotIn('OSS_ACCESS_KEY_SECRET', body['environmentVariables'])
        self.assertEqual(body['environmentVariables']['ATSUITE_CANDIDATE_ID'], 'candidate')
        client.create_function_with_options.side_effect = RuntimeError('cloud error')
        with self.assertRaises(RuntimeError):
            fn.create_function('repo@sha256:expected')
