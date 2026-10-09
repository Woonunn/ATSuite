import json
import tempfile
import unittest
from pathlib import Path

from atsuite.pipeline import resolve_benchmark
from atsuite.recommender.api import materialize_plan, plan_experiment
from atsuite.recommender.contracts import ExperimentSpec
from atsuite.recommender.topology import TopologyNode, compile_session_config, generate_topologies, generate_topology, nodes_from_config
from tests.test_recommender_candidates import make_config


class TopologyValidationTests(unittest.TestCase):
    def tearDown(self):
        resolve_benchmark.cache_clear()

    def test_invalid_topology_not_selected_and_compile_rechecks(self):
        nodes = [TopologyNode('a', must_separate=('b',)), TopologyNode('b')]
        invalid = generate_topology(nodes, 'all_in_one')
        self.assertFalse(invalid.valid)
        self.assertTrue(invalid.blocking_reasons)
        self.assertFalse(any(set(p.groups[0]) == {'a', 'b'} for p in generate_topologies(nodes)))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                compile_session_config(make_config(Path(tmp)), invalid)

    def test_colocate_atoms_unknown_state_and_topology_deduplication(self):
        nodes = [TopologyNode('a', must_colocate=('b',)), TopologyNode('b'), TopologyNode('c')]
        baseline = (('a', 'b', 'c'),)
        plans = generate_topologies(nodes, baseline_groups=baseline)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].groups, baseline)
        self.assertIn('per_node', plans[0].strategies)
        self.assertIn('unknown_state_semantics', plans[0].warnings)
        explicit = [TopologyNode('a', state_backend='stateless', must_colocate=('b',)),
                    TopologyNode('b', state_backend='stateless'), TopologyNode('c', state_backend='stateless')]
        split = generate_topology(explicit, 'per_node', baseline_groups=baseline)
        self.assertEqual(split.groups, (('a', 'b'), ('c',)))

    def test_unknown_references_and_python_conflicts_block(self):
        for nodes in ([TopologyNode('a', must_colocate=('missing',))],
                      [TopologyNode('a', python_version='3.10'), TopologyNode('b', python_version='3.11')]):
            plan = generate_topology(nodes, 'all_in_one')
            self.assertFalse(plan.valid)

    def test_metadata_retains_multiple_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            raw = json.loads(config.read_text())
            raw['nodes'][0]['trace_names'] = [{'name': 'tool.run', 'domain': 'left', 'access': 'rw'},
                                             {'name': 'tool.other', 'domain': 'right', 'access': 'r'}]
            raw['nodes'][0]['must_separate'] = ['other']
            config.write_text(json.dumps(raw))
            nodes, _ = nodes_from_config(config, 'gcp_mcp')
            self.assertEqual(nodes[0].state_domains, ('left', 'right'))
            self.assertEqual(nodes[0].must_separate, ('other',))

    def test_bundle_materialization_keeps_data_and_is_frozen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'benchmark'
            config = make_config(root)
            (root / 'data').mkdir()
            (root / 'data' / 'input.txt').write_text('frozen')
            spec = ExperimentSpec.from_dict({'benchmark': {'config': str(config)}, 'providers': ['gcp_faas']})
            plan = plan_experiment(spec)
            destination = Path(tmp) / 'result' / 'plan.json'
            materialize_plan(plan, destination)
            generated = Path(plan['candidates'][0]['generated_config'])
            resolved = resolve_benchmark(generated, 'gcp_faas')
            self.assertEqual((resolved.bench_root / 'data' / 'input.txt').read_text(), 'frozen')
            (root / 'data' / 'input.txt').write_text('changed')
            self.assertEqual((resolved.bench_root / 'data' / 'input.txt').read_text(), 'frozen')
            before = generated.read_bytes()
            with self.assertRaises(ValueError):
                materialize_plan(plan, destination)
            self.assertEqual(generated.read_bytes(), before)

    def test_duplicate_trace_route_rejected_and_source_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            raw = json.loads(config.read_text())
            raw['nodes'][0]['trace_names'] = ['tool.run', 'tool.run']
            config.write_text(json.dumps(raw))
            with self.assertRaises(SystemExit):
                resolve_benchmark(config, 'gcp_mcp')
            raw['nodes'][0]['trace_names'] = ['tool.run']
            raw['pipeline']['session']['servers'][0]['nodes'] = ['tool', 'tool']
            config.write_text(json.dumps(raw))
            with self.assertRaises(SystemExit):
                resolve_benchmark(config, 'gcp_mcp')
            raw['pipeline']['session']['servers'][0]['nodes'] = ['tool']
            config.write_text(json.dumps(raw))
            before = config.read_bytes()
            plan = plan_experiment(ExperimentSpec.from_dict({'benchmark': {'config': str(config)}, 'providers': ['gcp_mcp']}))
            self.assertEqual(config.read_bytes(), before)
            row = next(row for row in plan['candidates'] if row['baseline'])
            self.assertEqual(set(row['resources_by_target']), {'server'})

    def test_full_constraints_compile_revalidation_and_state_domain_atoms(self):
        nodes = [TopologyNode('a', state_domain='x', state_backend='memory'),
                 TopologyNode('b', state_domain='x', state_backend='memory'),
                 TopologyNode('c', state_backend='stateless')]
        self.assertEqual(generate_topology(nodes, 'per_node').groups, (('a', 'b'), ('c',)))
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            raw = json.loads(config.read_text())
            raw['nodes'][0]['must_separate'] = ['missing']
            config.write_text(json.dumps(raw))
            forged = generate_topology([TopologyNode('tool')])
            with self.assertRaises(ValueError):
                compile_session_config(config, forged)

    def test_tools_realizations_and_mcp_alias_normalized_with_constraints(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            raw = json.loads(config.read_text())
            raw['tools'] = [{'trace_name': 'tool.run', 'node': 'tool', 'impl_dir': './nodes/tool',
                             'access': 'stateless', 'isolation_class': 'strict'}]
            del raw['nodes']
            raw['realizations'] = raw.pop('pipeline')
            raw['realizations']['mcp_serverless'] = raw['realizations'].pop('session')
            config.write_text(json.dumps(raw))
            nodes, _ = nodes_from_config(config, 'gcp_mcp')
            self.assertEqual(nodes[0].isolation_class, 'strict')
            plan = plan_experiment(ExperimentSpec.from_dict({'benchmark': {'config': str(config)}, 'providers': ['gcp_mcp']}))
            materialize_plan(plan, Path(tmp) / 'results' / 'plan.json')
            compiled = json.loads(Path(plan['candidates'][0]['generated_config']).read_text())
            self.assertIn('session', compiled['pipeline'])
            self.assertEqual(compiled['nodes'][0]['isolation_class'], 'strict')
            self.assertTrue(resolve_benchmark(plan['candidates'][0]['generated_config'], 'gcp_mcp').targets)
