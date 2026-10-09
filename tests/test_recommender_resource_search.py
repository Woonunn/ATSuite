import json
import tempfile
import unittest
from pathlib import Path

from atsuite.pipeline import resolve_benchmark
from atsuite.recommender.api import materialize_plan, plan_experiment
from atsuite.recommender.resources import local_resource_candidates
from atsuite.recommender.capabilities import validate_candidate
from atsuite.recommender.contracts import CandidateSpec, ExperimentSpec
from tests.test_recommender_candidates import make_config


def candidate(provider, resources):
    return CandidateSpec('c', 'w', provider, 'faas', 'faas', 'us-central1', 'linux/amd64', (('tool',),), {'tool': resources})


class ResourceSearchTests(unittest.TestCase):
    def tearDown(self):
        resolve_benchmark.cache_clear()

    def test_legal_lambda_unsampled_baseline(self):
        raw = {'cpu': round(1536 / 1769, 3), 'memory_mb': 1536, 'disk_mb': 600, 'timeout_s': 40}
        self.assertTrue(validate_candidate(candidate('aws_lambda', raw)).valid)
        self.assertFalse(validate_candidate(candidate('aws_lambda', {**raw, 'cpu': 2})).valid)

    def test_gcp_fractional_limits_and_modes(self):
        base = {'cpu': .08, 'memory_mb': 8192, 'extras': {'concurrency': 1, 'billing_mode': 'request_based', 'execution_environment': 'gen1'}}
        self.assertFalse(validate_candidate(candidate('gcp_faas', base)).valid)
        legal = {**base, 'cpu': .5, 'memory_mb': 512}
        self.assertTrue(validate_candidate(candidate('gcp_faas', legal)).valid)
        self.assertFalse(validate_candidate(candidate('gcp_faas', {**legal, 'extras': {**legal['extras'], 'concurrency': 2}})).valid)

    def test_exact_family_baselines_round_robin_and_local_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            providers = ['gcp_faas', 'ali_fc', 'gcp_mcp']
            spec = ExperimentSpec.from_dict({'benchmark': {'config': str(config)}, 'providers': providers,
                                            'search': {'max_candidates': 3}})
            plan = plan_experiment(spec)
            self.assertEqual({row['provider'] for row in plan['candidates']}, set(providers))
            mcp = next(row for row in plan['candidates'] if row['provider'] == 'gcp_mcp')
            self.assertEqual(mcp['resources_by_target']['server']['cpu'], 2)
            self.assertEqual(mcp['resources_by_target']['server']['memory_mb'], 2048)
            faas = next(row for row in plan['candidates'] if row['provider'] == 'gcp_faas')
            self.assertEqual(faas['resources_by_target']['tool']['memory_mb'], 512)
            self.assertEqual(mcp['resource_source'], 'exact_baseline')
            limited = plan_experiment(ExperimentSpec.from_dict({'benchmark': {'config': str(config)}, 'providers': providers,
                'search': {'max_candidates': 2}}))
            self.assertFalse(limited['scope']['coverage_complete'])
            self.assertTrue(any(row['reason_code'] == 'skipped_by_budget' for row in limited['excluded']))

    def test_missing_resources_not_filled_and_no_effective_fabrication(self):
        self.assertFalse(validate_candidate(candidate('gcp_faas', {})).valid)
        valid = validate_candidate(candidate('gcp_faas', {'cpu': 1, 'memory_mb': 512}))
        self.assertTrue(valid.valid)
        self.assertIsNone(valid.effective)

    def test_three_heterogeneous_targets_change_one_coordinate(self):
        baseline = {name: {'cpu': cpu, 'memory_mb': memory, 'disk_mb': 512, 'timeout_s': timeout,
                           'architecture': 'linux/amd64', 'extras': {}}
                    for name, cpu, memory, timeout in [('io', 1, 512, 10), ('compute', 2, 2048, 40), ('state', 4, 4096, 80)]}
        rows = list(local_resource_candidates(baseline, 'gcp_mcp', limit_per_target=4))
        self.assertEqual(rows[0][0], baseline)
        for resources, audit in rows[1:]:
            changed = [name for name in baseline if baseline[name] != resources[name]]
            self.assertEqual(changed, [audit['changed_target']])
            for name in baseline:
                self.assertEqual(resources[name]['timeout_s'], baseline[name]['timeout_s'])

    def test_region_and_deployment_options_survive_resolution_and_no_cache_staleness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'benchmark'
            config = make_config(root)
            raw = json.loads(config.read_text())
            raw['pipeline']['faas']['units'][0]['deploy'].update(region='old-region', concurrency=4,
                billing_mode='request_based', execution_environment='gen2')
            config.write_text(json.dumps(raw))
            first = resolve_benchmark(config, 'gcp_faas')
            self.assertEqual(first.targets['tool'].runtime.memory, 512)
            raw['pipeline']['faas']['units'][0]['deploy']['memory'] = 768
            config.write_text(json.dumps(raw))
            self.assertEqual(resolve_benchmark(config, 'gcp_faas').targets['tool'].runtime.memory, 768)
            spec = ExperimentSpec.from_dict({'benchmark': {'config': str(config)}, 'providers': ['gcp_faas'],
                'regions': {'gcp_faas': 'us-central1'}})
            plan = plan_experiment(spec)
            materialize_plan(plan, Path(tmp) / 'result' / 'plan.json')
            deployed = resolve_benchmark(plan['candidates'][0]['generated_config'], 'gcp_faas').targets['tool'].runtime
            self.assertEqual(deployed.region, 'us-central1')
            self.assertEqual(deployed.concurrency, 4)
            self.assertEqual(deployed.execution_environment, 'gen2')

    def test_ali_adapter_modes_and_steps(self):
        from atsuite.recommender.capabilities import describe_capability
        capability = describe_capability('ali_agentrun', 'cn-hangzhou')
        self.assertEqual(capability.deployment_mode, 'custom_container_session')
        self.assertEqual(capability.cpu_mode, 'independent')
        valid = {'cpu': .5, 'memory_mb': 768}
        self.assertTrue(validate_candidate(candidate('ali_fc', valid)).valid)
        for raw in ({**valid, 'memory_mb': 769}, {**valid, 'cpu': .53},
                    {**valid, 'memory_mb': 8192}, {**valid, 'disk_mb': 600}):
            self.assertFalse(validate_candidate(candidate('ali_fc', raw)).valid)
