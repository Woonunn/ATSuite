import copy
import json
import tempfile
import unittest
from pathlib import Path

from atsuite.recommender.api import materialize_child_candidate, materialize_plan, plan_experiment
from atsuite.recommender.contracts import ExperimentSpec
from atsuite.recommender.identity import digest
from tests.test_recommender_candidates import make_config


class ChildPlanningTests(unittest.TestCase):
    def test_child_uses_frozen_bundle_and_parent_remains_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_config(root / 'benchmark')
            (config.parent.parent / 'data').mkdir()
            data = config.parent.parent / 'data' / 'value.txt'
            data.write_text('frozen')
            plan = plan_experiment(ExperimentSpec.from_dict({'benchmark': {'config': str(config)}, 'providers': ['gcp_faas']}))
            materialize_plan(plan, root / 'parent' / 'plan.json')
            before = copy.deepcopy(plan)
            data.write_text('new source contents')
            child = materialize_child_candidate(plan, {'provider': 'gcp_faas', 'resources_by_target': {
                'tool': {'cpu': 1, 'memory_mb': 768, 'disk_mb': 512}}}, root / 'children' / 'one' / 'plan.json')
            self.assertEqual(plan, before)
            self.assertEqual(child['parent_plan_digest'], digest(plan, length=64))
            self.assertEqual((Path(child['bundle_root']) / 'data' / 'value.txt').read_text(), 'frozen')
            self.assertEqual(child['resources_by_target']['tool']['memory_mb'], 768)
            self.assertEqual(child['workload_fingerprint'], plan['candidates'][0]['workload_fingerprint'])
            self.assertEqual(child['bundle_digest'], plan['bundle_digest'])

    def test_out_of_scope_missing_targets_and_illegal_resource_proposals_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_config(root / 'benchmark')
            plan = plan_experiment(ExperimentSpec.from_dict({'benchmark': {'config': str(config)}, 'providers': ['gcp_mcp']}))
            materialize_plan(plan, root / 'parent' / 'plan.json')
            for index, proposal in enumerate((
                {'provider': 'aws_lambda'}, {'provider': 'gcp_mcp', 'region': 'unexpected'},
                {'provider': 'gcp_mcp', 'permissions': ['admin']},
                {'provider': 'gcp_mcp', 'logical_topology': [['missing']]},
                {'provider': 'gcp_mcp', 'logical_topology': [['tool', 'tool']]},
                {'provider': 'gcp_mcp', 'resources_by_target': {'wrong': {'cpu': 1, 'memory_mb': 512}}},
                {'provider': 'gcp_mcp', 'resources_by_target': {'server': {'cpu': .08, 'memory_mb': 8192}}},
            )):
                with self.subTest(proposal=proposal), self.assertRaises(ValueError):
                    materialize_child_candidate(plan, proposal, root / 'children' / str(index) / 'plan.json')

    def test_proposal_capacity_reserved_without_losing_provider_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            plan = plan_experiment(ExperimentSpec.from_dict({'benchmark': {'config': str(config)},
                'providers': ['gcp_faas', 'gcp_mcp'], 'search': {'max_candidates': 3}}))
            self.assertEqual(plan['scope']['reserved_candidate_slots'], 1)
            self.assertEqual(len(plan['candidates']), 2)
            self.assertEqual({candidate['provider'] for candidate in plan['candidates']}, {'gcp_faas', 'gcp_mcp'})
            self.assertEqual(len(plan['withheld_candidate_proposals']), 1)
            materialize_plan(plan, Path(tmp) / 'parent' / 'plan.json')
            child = materialize_child_candidate(plan, plan['withheld_candidate_proposals'][0], Path(tmp) / 'children' / 'fallback' / 'plan.json')
            self.assertNotIn(child['candidate_id'], {candidate['candidate_id'] for candidate in plan['candidates']})
