import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from atsuite.pipeline import logical_state_domain, resolve_benchmark
from atsuite.recommender.api import plan_experiment
from atsuite.recommender.contracts import ExperimentSpec
from atsuite.state_snapshot import maybe_attach_agentcore_state_snapshot, seed_alifc_state_snapshot_for_uid, seed_gcp_state_snapshot_for_uid
from atsuite_sdk.identity import StateKey


class StateDomainRoutingTests(unittest.TestCase):
    def test_gcp_session_snapshot_attaches_once_by_logical_domain(self):
        bundle = {'services': {'counter': {'snapshot': {'module': {'value': 3}}}}}
        seen = set()
        args = maybe_attach_agentcore_state_snapshot({'uid': 'trial'}, provider='gcp_mcp', target_name='counter',
            uid='trial', state_snapshot_bundle=bundle, seeded_targets=seen)
        self.assertEqual(args.get('__atsuite_state_snapshot'), bundle['services']['counter']['snapshot'])
        self.assertEqual(seen, {'counter'})
        self.assertNotIn('__atsuite_state_snapshot', maybe_attach_agentcore_state_snapshot({'uid': 'trial'}, provider='gcp_mcp',
            target_name='counter', uid='trial', state_snapshot_bundle=bundle, seeded_targets=seen))

    def test_ali_snapshot_seeding_replaces_and_uses_sdk_identity(self):
        storage = mock.Mock()
        identity = {'tenant': 'tenant', 'experiment': 'experiment', 'candidate': 'candidate', 'scenario': 'scenario'}
        services = seed_alifc_state_snapshot_for_uid(provider='ali_fc', uid='trial', bucket='preexisting', client=storage,
            state_context=identity, state_snapshot_bundle={'services': {'counter': {'snapshot': {'module': {'value': 3}}}}})
        expected = StateKey('tenant', 'experiment', 'candidate', 'scenario', 'counter', 'trial').path()
        self.assertEqual(services, {'counter'})
        storage.clearobj.assert_called_once_with(expected)
        self.assertEqual(storage.append.call_args.args[0], expected)
        self.assertEqual(json.loads(storage.append.call_args.args[1]), {'module': {'value': 3}})

    def test_gcp_faas_snapshot_seeding_preserves_identity_and_propagates_storage_error(self):
        storage = mock.Mock()
        identity = {'tenant': 'tenant', 'experiment': 'experiment', 'candidate': 'candidate', 'scenario': 'scenario'}
        bundle = {'services': {'counter': {'snapshot': {'module': {'value': 3}}}}}
        self.assertEqual(seed_gcp_state_snapshot_for_uid(provider='gcp_faas', uid='trial', client=storage,
            state_context=identity, state_snapshot_bundle=bundle), {'counter'})
        expected = StateKey('tenant', 'experiment', 'candidate', 'scenario', 'counter', 'trial').path()
        storage.clearobj.assert_called_once_with(expected)
        self.assertEqual(json.loads(storage.append.call_args.args[1]), bundle['services']['counter']['snapshot'])
        storage.clearobj.side_effect = PermissionError('not authorized')
        with self.assertRaises(PermissionError):
            seed_gcp_state_snapshot_for_uid(provider='gcp_faas', uid='trial', client=storage,
                state_context=identity, state_snapshot_bundle=bundle)

    def test_single_domain_does_not_follow_target_rename(self):
        route = SimpleNamespace(target_name='renamed_server', domain='business_domain', access='rw')
        resolved = SimpleNamespace(routes={'a.run': route}, targets={'renamed_server': SimpleNamespace()})
        self.assertEqual(logical_state_domain(resolved, 'renamed_server'), 'business_domain')

    def test_multidomain_baseline_retained_and_regrouping_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'config').mkdir()
            (root / 'trace').mkdir()
            nodes = []
            for name, domain in (('a', 'left'), ('b', 'right')):
                (root / 'nodes' / name).mkdir(parents=True)
                nodes.append({'name': name, 'dir': './nodes/' + name, 'state_backend': 'memory',
                    'trace_names': [{'name': name + '.run', 'domain': domain, 'access': 'rw'}]})
            (root / 'trace' / 'trace.json').write_text(json.dumps({'nodes': [
                {'id': 0, 'name': 'start', 'type': 'logic', 'edge_to': [{'id': 1}]},
                {'id': 1, 'name': 'a.run', 'type': 'tool_use', 'edge_to': [{'id': 2}]},
                {'id': 2, 'name': 'b.run', 'type': 'tool_use', 'edge_to': []}]}))
            config = root / 'config' / 'benchmark.json'
            config.write_text(json.dumps({'trace_file': './trace/trace.json', 'nodes': nodes,
                'pipeline': {'session': {'servers': [{'name': 'original', 'nodes': ['a', 'b'],
                    'deploy': {'cpu': 1, 'memory': 1024}}]}}}))
            resolved = resolve_benchmark(config, 'gcp_mcp')
            self.assertEqual(logical_state_domain(resolved, 'original'), 'original')
            plan = plan_experiment(ExperimentSpec.from_dict({'benchmark': {'config': str(config)}, 'providers': ['gcp_mcp']}))
            self.assertTrue(plan['candidates'])
            self.assertTrue(all(row['logical_topology'] == (('a', 'b'),) for row in plan['candidates']))
            self.assertTrue(any(row['reason_code'] == 'ambiguous_state_regrouping' for row in plan['excluded']))
