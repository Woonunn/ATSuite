import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from atsuite.recommender.correctness import compare_outputs, validate_trial
from atsuite.pipeline import resolve_benchmark
from atsuite_sdk import state as state_module
from atsuite_sdk.abstract import registry
from atsuite_sdk.identity import StateKey
from atsuite_sdk.state import StateRuntime
from atsuite.runtime import InvocationResult, RuntimeSession
from atsuite.invoker import run_trace


class OracleBenchmarkTests(unittest.TestCase):
    def test_real_registered_tools_restore_state_per_uid_and_oracle_rejects_corruption(self):
        root = Path(__file__).resolve().parents[1] / 'benchmarks' / 'RecommenderOracle'
        oracle = json.loads((root / 'oracle' / 'exact.json').read_text())
        before = dict(registry.functions)
        previous = state_module._RUNTIME
        module_name = 'atsuite_counter_oracle_fixture'
        try:
            with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                'ATSUITE_RUNTIME': 'mcp', 'ATSUITE_SESSION_ROOT': tmp, 'ATSUITE_TENANT': 'tenant',
                'ATSUITE_EXPERIMENT_ID': 'experiment', 'ATSUITE_CANDIDATE_ID': 'candidate',
                'ATSUITE_SCENARIO_ID': 'scenario', 'ATSUITE_LOGICAL_STATE_DOMAIN': 'counter'}):
                runtime = StateRuntime()
                state_module._RUNTIME = runtime
                spec = importlib.util.spec_from_file_location(module_name, root / 'nodes' / 'counter' / 'implementation.py')
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                actual = {
                    'counter.add_first': module.counter_add.execute_from_dict({'uid': 'trial-a', 'amount': 2}),
                    'counter.add_second': module.counter_add.execute_from_dict({'uid': 'trial-a', 'amount': 3}),
                    'counter.snapshot': module.counter_snapshot.execute_from_dict({'uid': 'trial-a'})}
                self.assertTrue(compare_outputs(oracle['expected_outputs'], actual))
                self.assertTrue(validate_trial(expected_state=oracle['expected_state'], actual_state=actual['counter.snapshot'],
                    require_output=False, require_state=True).passed)
                self.assertEqual(module.counter_add.execute_from_dict({'uid': 'trial-b', 'amount': 2}), {'total': 2})
                self.assertEqual(module.counter_snapshot.execute_from_dict({'uid': 'trial-b'}), {'total': 2, 'amounts': [2]})
                self.assertEqual(module.counter_snapshot.execute_from_dict({'uid': 'trial-a'}), oracle['expected_state'])
                self.assertFalse(validate_trial(expected_state=oracle['expected_state'], actual_state={'total': 999, 'amounts': []},
                    require_output=False, require_state=True).passed)
                key = StateKey('tenant', 'experiment', 'candidate', 'scenario', 'counter', 'trial-a').path()
                self.assertEqual(runtime._snapshot_key('trial-a'), key)
                self.assertNotEqual(key, StateKey('other', 'experiment', 'candidate', 'scenario', 'counter', 'trial-a').path())
                self.assertNotEqual(key, StateKey('tenant', 'experiment', 'other', 'scenario', 'counter', 'trial-a').path())
        finally:
            registry.functions.clear()
            registry.functions.update(before)
            state_module._RUNTIME = previous
            sys.modules.pop(module_name, None)

    def test_workflow_routes_same_logical_domain_for_faas_and_session(self):
        config = Path(__file__).resolve().parents[1] / 'benchmarks' / 'RecommenderOracle' / 'config' / 'benchmark.json'
        for provider in ('gcp_faas', 'gcp_mcp', 'aws_lambda', 'aws_agentcore', 'ali_fc', 'ali_agentrun'):
            resolved = resolve_benchmark(config, provider)
            self.assertEqual(set(resolved.targets), {'counter'})
            self.assertEqual({route.domain for route in resolved.routes.values()}, {'counter'})
            self.assertEqual(resolved.routes['counter.snapshot'].tool_name, 'counter_snapshot')

    def test_real_workflow_replay_and_cleanup_use_logical_domain(self):
        root = Path(__file__).resolve().parents[1] / 'benchmarks' / 'RecommenderOracle'
        oracle = json.loads((root / 'oracle' / 'exact.json').read_text())
        previous, registered = state_module._RUNTIME, dict(registry.functions)
        module_name = 'atsuite_counter_replay_fixture'
        try:
            with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                'ATSUITE_RUNTIME': 'mcp', 'ATSUITE_SESSION_ROOT': tmp, 'ATSUITE_LOGICAL_STATE_DOMAIN': 'counter'}):
                state_module._RUNTIME = StateRuntime()
                source = importlib.util.spec_from_file_location(module_name, root / 'nodes' / 'counter' / 'implementation.py')
                module = importlib.util.module_from_spec(source)
                sys.modules[module_name] = module
                source.loader.exec_module(module)
                class FakeCloudTransport:
                    def connect(self, endpoints): pass
                    def open_session(self, target, uid): return RuntimeSession(uid, target.target_id)
                    def invoke(self, request):
                        output = registry.functions[request.tool_name].execute_from_dict(request.args)
                        return InvocationResult(result=output, provider_metadata={'is_stateful': True})
                    def cleanup_run(self, uid): pass
                    def close_session(self, session): pass
                config = json.loads((root / 'config' / 'benchmark.json').read_text())
                config['trace_file'] = str(root / 'trace' / 'workflow.json')
                config['nodes'][0]['dir'] = str(root / 'nodes' / 'counter')
                config['pipeline']['faas']['units'][0]['name'] = 'renamed_target'
                config['recommender'] = {'baseline_state_domains': {'original_target': 'counter'}}
                source_config = Path(tmp) / 'config.json'
                source_config.write_text(json.dumps(config))
                url_map = Path(tmp) / 'urls.json'
                initial = {'services': {'original_target': {'snapshot': {}}}}
                for provider in ('aws_lambda', 'ali_fc', 'gcp_faas'):
                    with self.subTest(provider=provider):
                        url_map.write_text(json.dumps({'provider': provider, 'targets': {'renamed_target': {'endpoint': 'https://fake.example'}}}))
                        with mock.patch('atsuite.invoker.create_runtime_adapter', return_value=FakeCloudTransport()), mock.patch('atsuite.invoker.cleaner', return_value=0.0) as cleaner, mock.patch('atsuite.invoker.seed_lambda_state_snapshot_for_uid', return_value={'counter'}) as aws_seed, mock.patch('atsuite.invoker.seed_alifc_state_snapshot_for_uid', return_value={'counter'}) as ali_seed, mock.patch('atsuite.invoker.seed_gcp_state_snapshot_for_uid', return_value={'counter'}) as gcp_seed:
                            replay = run_trace(source_config, url_map, 'trial-' + provider, provider=provider, output_dir=Path(tmp) / 'outputs',
                                skip_analyzer=True, raise_on_error=False, state_snapshot_bundle=initial)
                        self.assertEqual(replay['status'], 'success', replay.get('error'))
                        outputs = json.loads(Path(replay['outputs_path']).read_text())
                        observed = {row['node_name']: row['output'] for row in outputs.values()}
                        self.assertTrue(compare_outputs(oracle['expected_outputs'], observed))
                        self.assertEqual(cleaner.call_args.args[2], {'counter'})
                        seed = {'aws_lambda': aws_seed, 'ali_fc': ali_seed, 'gcp_faas': gcp_seed}[provider]
                        self.assertEqual(seed.call_args.kwargs['provider'], provider)
                        self.assertEqual(seed.call_args.kwargs['state_snapshot_bundle'], {'services': {'counter': {'snapshot': {}}}})
                        self.assertEqual(initial, {'services': {'original_target': {'snapshot': {}}}})
        finally:
            registry.functions.clear()
            registry.functions.update(registered)
            state_module._RUNTIME = previous
            sys.modules.pop(module_name, None)
