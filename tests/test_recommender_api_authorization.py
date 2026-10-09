import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from atsuite.server.app import create_app
from atsuite.server.security import safe_extract_zip


class ControlPlaneRegressionTests(unittest.TestCase):
    def test_client_headers_do_not_grant_admin_or_change_tenant(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(artifact_root=Path(tmp))
            client = TestClient(app, base_url="http://localhost")
            response = client.get('/api/v1/principal', headers={
                'X-ATSuite-Role': 'admin', 'X-ATSuite-Tenant': '../../outside'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['role'], 'editor')
            self.assertEqual(response.json()['tenant'], 'local')

    def test_web_plan_refuses_arbitrary_server_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(artifact_root=Path(tmp)), base_url="http://localhost")
            response = client.post('/api/v1/experiments/plan', json={
                'schema_version': 1, 'benchmark': {'config': '/etc/passwd'},
                'providers': ['aws_lambda']})
            self.assertEqual(response.status_code, 400)
            self.assertIn('benchmark_id', response.text)

    def test_nested_reference_validation_is_transactional(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('b/config/x.json', json.dumps({'trace_file': '../../../outside.json', 'nodes': []}))
                bundle.writestr('b/trace/x.json', '{}')
            target = root / 'destination'
            with self.assertRaises(ValueError):
                safe_extract_zip(archive, target)
            self.assertFalse(target.exists())

    def test_failed_archive_leaves_no_partial_executable_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('b/nodes/run.py', 'raise RuntimeError("uploaded")')
                bundle.writestr('../escape', 'x')
            target = root / 'destination'
            with self.assertRaises(ValueError):
                safe_extract_zip(archive, target)
            self.assertFalse(target.exists())

    def test_scoped_tokens_expiry_audience_and_viewer_permissions(self):
        tokens = {
            'a' * 32: {'subject': 'viewer', 'tenant': 't', 'project': 'p', 'role': 'viewer'},
            'b' * 32: {'subject': 'expired', 'tenant': 't', 'project': 'p', 'role': 'admin', 'expires_at': 1},
            'c' * 32: {'subject': 'wrong', 'tenant': 't', 'project': 'p', 'role': 'admin', 'audience': 'elsewhere'},
        }
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(artifact_root=tmp, identity_tokens=tokens), base_url='http://localhost')
            self.assertEqual(client.get('/api/v1/principal').status_code, 401)
            for token in ('b', 'c'):
                self.assertEqual(client.get('/api/v1/principal', headers={'Authorization': 'Bearer ' + token * 32}).status_code, 401)
            spoof = {'Authorization': 'Bearer ' + 'a' * 32, 'X-ATSuite-Role': 'admin', 'X-ATSuite-Tenant': 'other'}
            self.assertEqual(client.get('/api/v1/principal', headers=spoof).json()['tenant'], 't')
            self.assertEqual(client.post('/api/v1/benchmarks', headers=spoof, content=b'x').status_code, 403)

    def test_production_and_non_loopback_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'identity provider'):
                create_app(artifact_root=tmp, mode='production')
            with self.assertRaisesRegex(ValueError, 'isolated builder'):
                create_app(artifact_root=tmp, mode='production', identity_tokens={
                    'a' * 32: {'subject': 'u', 'tenant': 't', 'project': 'p', 'role': 'editor'}})
            client = TestClient(create_app(artifact_root=tmp), base_url='http://evil.example')
            self.assertEqual(client.get('/api/v1/principal').status_code, 403)

    def test_upload_limit_checks_stream_without_publishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(artifact_root=tmp), base_url='http://localhost')
            with mock.patch('atsuite.server.security.MAX_ARCHIVE_BYTES', 5):
                response = client.post('/api/v1/benchmarks', content=iter([b'123', b'456']))
                self.assertEqual(response.status_code, 413)
            self.assertEqual(list((Path(tmp) / 'benchmarks').iterdir()), [])
            self.assertEqual(list(Path(tmp).glob('*.zip')), [])

    def test_production_health_failure_rolls_back_exact_snapshot(self):
        from atsuite.server.production import apply_change, prepare_change
        from atsuite.recommender.identity import digest
        class Adapter:
            def __init__(self): self.current = {'tenant': 't', 'project': 'p', 'value': 'old'}
            def inspect(self, target): return dict(self.current)
            def apply(self, target, config): self.current = {**self.current, **config}; return dict(self.current)
            def healthy(self, target): return False
            def restore(self, target, snapshot): self.current = dict(snapshot); return dict(snapshot)
        adapter = Adapter()
        config = {'value': 'new'}
        record = {'id': 'exp', 'recommendation': {'best_verified': 'c', 'status': 'recommended'},
                  'plan': {'plan_digest': 'frozen', 'candidates': [{'candidate_id': 'c', 'config_digest': digest(config, length=64), 'recommended_config': config}]}}
        change = prepare_change(record, 'c', 'target', adapter, tenant='t', project='p')
        approval = {'deployment_plan_id': change['deployment_plan_id'], 'config_digest': change['config_digest']}
        result = apply_change(change, approval, adapter)
        self.assertEqual(result['status'], 'rolled_back')
        self.assertEqual(adapter.current, change['snapshot'])
        with self.assertRaises(ValueError):
            prepare_change(record, 'unverified', 'target', adapter, tenant='t', project='p')
        adapter.current['value'] = 'drift'
        with self.assertRaisesRegex(ValueError, 'drift'):
            apply_change(change, approval, adapter)

    def test_production_api_requires_extra_approval_and_preserves_experiment(self):
        from atsuite.recommender.identity import digest
        class Adapter:
            def __init__(self): self.current = {'tenant': 't', 'project': 'p', 'value': 'old'}
            def inspect(self, target): return dict(self.current)
            def apply(self, target, config): self.current = {**self.current, **config}; return dict(self.current)
            def healthy(self, target): return False
            def restore(self, target, snapshot): self.current = dict(snapshot); return dict(snapshot)
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(artifact_root=tmp, production_adapter=Adapter(), identity_tokens={
                'a' * 32: {'subject': 'admin', 'tenant': 't', 'project': 'p', 'role': 'admin'}})
            client = TestClient(app, base_url='http://localhost')
            headers = {'Authorization': 'Bearer ' + 'a' * 32}
            config = {'value': 'new'}
            record = app.state.store.create('exp', {'tenant': 't', 'project': 'p',
                'recommendation': {'best_verified': 'c', 'status': 'recommended'},
                'plan': {'plan_digest': 'frozen', 'candidates': [{'candidate_id': 'c',
                    'config_digest': digest(config, length=64), 'recommended_config': config}]}}, state='COMPLETED')
            denied = client.post('/api/v1/deployments/plan', headers=headers,
                                 json={'experiment_id': 'exp', 'candidate_id': 'unverified', 'target_id': 'target'})
            self.assertEqual(denied.status_code, 409)
            change = client.post('/api/v1/deployments/plan', headers=headers,
                                 json={'experiment_id': 'exp', 'candidate_id': 'c', 'target_id': 'target'})
            self.assertEqual(change.status_code, 201, change.text)
            change = change.json()
            self.assertEqual(client.post('/api/v1/deployments/apply', headers=headers,
                json={'confirm': True, 'deployment_plan_id': change['deployment_plan_id']}).status_code, 403)
            approval = client.post('/api/v1/deployments/approve', headers=headers,
                json={'confirm': True, 'deployment_plan_id': change['deployment_plan_id'], 'config_digest': change['config_digest']})
            self.assertEqual(approval.status_code, 201)
            result = client.post('/api/v1/deployments/apply', headers=headers,
                                 json={'approval_id': approval.json()['approval_id']})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['status'], 'rolled_back')
            self.assertEqual(app.state.store.get('exp'), record)
            self.assertEqual(client.post('/api/v1/deployments/apply', headers=headers,
                                 json={'approval_id': approval.json()['approval_id']}).status_code, 403)

    def test_evidence_endpoint_checks_content_hash_and_tenant(self):
        from atsuite.recommender.identity import digest
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(artifact_root=tmp, identity_tokens={
                'a' * 32: {'subject': 'one', 'tenant': 't', 'project': 'p', 'role': 'viewer'},
                'b' * 32: {'subject': 'two', 'tenant': 'other', 'project': 'p', 'role': 'viewer'}})
            client = TestClient(app, base_url='http://localhost')
            identity = {'candidate_id': 'c', 'workload_fingerprint': 'w', 'scenario_id': 's'}
            artifact = {**identity, 'kind': 'confirmation_trials', 'trials': []}
            ref = 'sha256:' + digest(artifact, length=64)
            evidence = {**identity, 'artifacts': {ref: artifact}}
            app.state.store.create('exp', {'tenant': 't', 'project': 'p', 'evidence_candidates': [evidence]})
            url = '/api/v1/experiments/exp/evidence/' + ref
            owner = {'Authorization': 'Bearer ' + 'a' * 32}
            self.assertEqual(client.get(url, headers=owner).json(), artifact)
            self.assertEqual(client.get(url, headers={'Authorization': 'Bearer ' + 'b' * 32}).status_code, 404)
            evidence['artifacts'][ref] = {**artifact, 'kind': 'tampered'}
            app.state.store.checkpoint('exp', 'CREATED', {'evidence_candidates': [evidence]})
            self.assertEqual(client.get(url, headers=owner).status_code, 409)

    def test_builtin_lambda_production_adapter_contract(self):
        from atsuite.server.production import AWSLambdaProductionAdapter
        class SDK:
            def __init__(self):
                self.image = 'registry.example/runtime@sha256:' + 'a' * 64
                self.configuration = {'FunctionArn': 'arn:owned', 'MemorySize': 1769, 'Timeout': 30,
                                      'EphemeralStorage': {'Size': 512}, 'Architectures': ['x86_64']}
                self.updates = []
            def get_function(self, **kw):
                return {'Configuration': dict(self.configuration), 'Code': {'ResolvedImageUri': self.image}}
            def list_tags(self, **kw): return {'Tags': {'atsuite:tenant': 't', 'atsuite:project': 'p'}}
            def update_function_code(self, **kw): self.updates.append(kw); self.image = kw['ImageUri']
            def update_function_configuration(self, **kw):
                self.updates.append(kw); self.configuration.update({k: v for k, v in kw.items() if k != 'FunctionName'})
            def get_waiter(self, name): return mock.Mock()
            def invoke(self, **kw): return {'StatusCode': 200, 'Payload': io.BytesIO(b'{"healthy":true}')}
        sdk = SDK()
        adapter = AWSLambdaProductionAdapter({'owned': {'function_name': 'owned-function', 'tenant': 't', 'project': 'p',
            'region': 'us-east-1', 'health_payload': {'health': True}, 'health_expected': {'healthy': True}}}, client=sdk)
        snapshot = adapter.inspect('owned')
        changed = adapter.apply('owned', {'resources_by_target': {'unit': {'memory_mb': 2048, 'timeout_s': 60, 'disk_mb': 1024,
            'architecture': 'linux/amd64'}}, 'published_artifacts': {'unit': {'repository': 'registry.example/runtime:tag',
            'digest': 'sha256:' + 'b' * 64}}})
        self.assertEqual(changed['memory_mb'], 2048)
        self.assertTrue(adapter.healthy('owned'))
        self.assertEqual(adapter.restore('owned', snapshot), snapshot)
        self.assertTrue(all(call['FunctionName'] == 'owned-function' for call in sdk.updates))
        with self.assertRaises(PermissionError):
            adapter.inspect('arbitrary-arn')


if __name__ == '__main__':
    unittest.main()
