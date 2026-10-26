import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from atsuite import invoker
from atsuite.runtime import InvocationResult, RuntimeCapabilities, RuntimeSession
from atsuite.state_snapshot import seed_lambda_state_snapshot_for_uid
from atsuite.utils import cleaner, state_bucket_for_provider


class StateBucketConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch('atsuite.utils.load_dotenv').start()
        mock.patch.dict(os.environ, {'ATSUITE_STATE_BUCKETS': json.dumps({
            'aws_lambda': 'uaibs', 'aws_agentcore': 'uaibs',
            'ali_fc': 'wabber', 'ali_agentrun': 'wabber',
        })}).start()

    def test_provider_mapping_and_legacy_defaults(self):
        for provider, expected in [('aws_lambda', 'uaibs'), ('aws_agentcore', 'uaibs'),
                                   ('ali_fc', 'wabber'), ('ali_agentrun', 'wabber')]:
            self.assertEqual(state_bucket_for_provider(provider), expected)
        with mock.patch.dict(os.environ, {'ATSUITE_STATE_BUCKETS': '{}'}):
            self.assertEqual(state_bucket_for_provider('aws_lambda'), 'atsuite')
            self.assertEqual(state_bucket_for_provider('ali_fc'), 'atsuite11')

    def test_invalid_bucket_config_fails_before_storage_access(self):
        for value in ['not json', '[]', '{"aws_lambda": null}', '{"aws_lambda": ""}']:
            with self.subTest(value=value), mock.patch.dict(os.environ, {'ATSUITE_STATE_BUCKETS': value}):
                with self.assertRaises(ValueError):
                    state_bucket_for_provider('aws_lambda')

    def test_cleanup_honors_selected_oss_bucket(self):
        with mock.patch('atsuite.ali.oss.AliOSS') as storage:
            storage.return_value.getobjsize.return_value = 0.25
            self.assertEqual(cleaner('ali_fc', 'wabber', {'server'}, 'uid'), 0.25)
        storage.assert_called_once_with('wabber')
        storage.return_value.deleteobj.assert_called_once_with('server/uid.json')

    def test_cleanup_honors_selected_s3_bucket(self):
        with mock.patch('boto3.client') as client:
            client.return_value.head_object.return_value = {'ContentLength': 1024}
            cleaner('aws_lambda', 'uaibs', {'server'}, 'uid')
        client.return_value.delete_object.assert_called_once_with(Bucket='uaibs', Key='server/uid.json')

    def test_snapshot_uses_configured_bucket_and_allows_explicit_override(self):
        snapshot = {'services': {'server': {'snapshot': {'vars': {}, 'files': {}}}}}
        for override, expected in [(None, 'uaibs'), ('explicit-bucket', 'explicit-bucket')]:
            client = mock.Mock()
            seeded = seed_lambda_state_snapshot_for_uid(
                provider='aws_lambda', uid='uid', state_snapshot_bundle=snapshot,
                bucket=override, s3_client=client,
            )
            self.assertEqual(seeded, {'server'})
            self.assertEqual(client.put_object.call_args.kwargs['Bucket'], expected)
            self.assertEqual(client.put_object.call_args.kwargs['Key'], 'server/uid.json')

    def test_replay_uses_same_bucket_for_snapshot_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'nodes/tool').mkdir(parents=True)
            config = root / 'config.json'
            (root / 'trace.json').write_text(json.dumps({'nodes': [
                {'id': 0, 'name': 'start', 'type': 'logic', 'edge_to': [{'id': 1}]},
                {'id': 1, 'name': 'tool.run', 'type': 'tool_use', 'edge_to': []},
            ]}))
            config.write_text(json.dumps({
                'trace_file': './trace.json',
                'nodes': [{'name': 'tool', 'dir': './nodes/tool',
                           'trace_names': [{'name': 'tool.run', 'tool': 'tool_run', 'stateful': True}]}],
                'pipeline': {'faas': {'units': [{'name': 'server', 'node': 'tool', 'trace_names': ['tool.run'], 'deploy': {'cpu': 1, 'memory': 128, 'disk': 512, 'timeout': 30}}]}}
            }))
            url_map = root / 'urls.json'
            runtime = mock.Mock()
            runtime.capabilities.return_value = RuntimeCapabilities('faas', False, False, True)
            runtime.open_session.return_value = RuntimeSession('uid', 'server')
            runtime.invoke.return_value = InvocationResult(client_elapsed_ms=1)
            cwd = Path.cwd()
            try:
                os.chdir(root)
                for provider, bucket in [('aws_lambda', 'uaibs'), ('ali_fc', 'wabber')]:
                    url_map.write_text(json.dumps({'provider': provider, 'targets': {'server': 'https://example.test'}}))
                    with mock.patch.object(invoker, 'create_runtime_adapter', return_value=runtime), \
                         mock.patch.object(invoker, 'seed_lambda_state_snapshot_for_uid', return_value=set()) as seed, \
                         mock.patch.object(invoker, 'cleaner', return_value=0) as cleanup, \
                         contextlib.redirect_stdout(io.StringIO()):
                        result = invoker.run_trace(config, url_map, 'uid', provider=provider,
                                                  skip_sleep=True, skip_analyzer=True)
                    self.assertEqual(result['status'], 'ok')
                    self.assertEqual(seed.call_args.kwargs['bucket'], bucket)
                    self.assertEqual(cleanup.call_args.args[1], bucket)
                    self.assertIn('server', cleanup.call_args.args[2])
            finally:
                os.chdir(cwd)
