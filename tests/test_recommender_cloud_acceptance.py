"""Local acceptance-tool contracts; these tests never execute cloud operations."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tools.recommender_cloud_acceptance import main, acceptance_checks


class CloudAcceptanceContracts(unittest.TestCase):
    def test_each_service_can_freeze_a_plan_without_cloud_dispatch(self):
        config=Path(__file__).resolve().parents[1]/'benchmarks/RecommenderOracle/config/benchmark.json'
        with tempfile.TemporaryDirectory() as tmp, patch('atsuite.recommender.service.run') as dispatch:
            for provider in ('aws_lambda','aws_agentcore','gcp_faas','gcp_mcp','ali_fc','ali_agentrun'):
                with self.subTest(provider=provider),contextlib.redirect_stdout(io.StringIO()):
                    output=Path(tmp)/provider
                    self.assertEqual(main(['--config',str(config),'--provider',provider,'--region','us-east-1',
                        '--account','explicit-contract-account','--max-spend','5','--max-wall-seconds','120',
                        '--output',str(output)]),0)
                    plan=json.loads((output/'plan.json').read_text())
                    self.assertTrue(plan['candidates'])
                    self.assertTrue(all(c['provider']==provider for c in plan['candidates']))
            dispatch.assert_not_called()

    def test_single_managed_candidate_can_verify_service_but_empty_or_simulated_cannot(self):
        # Claimed real source is an artificial unit fixture for classification;
        # no acceptance.json is published and this never counts as live evidence.
        result={'state':'COMPLETED','cleanup_status':'success',
            'evidence_candidates':[{'candidate_id':'managed','measurement_source':'platform_observed'}],
            'recommendation':{'eligibility':[{'candidate_id':'managed','eligible':True}]},
            'published_artifacts':{'managed':{'target':{'registry_verified':True,'digest':'sha256:'+'a'*64}}}}
        self.assertTrue(all(acceptance_checks(result).values()))
        result['evidence_candidates'][0]['measurement_source']='simulated'
        self.assertFalse(acceptance_checks(result)['real_observations'])
        result['published_artifacts']={'managed':{}}
        self.assertFalse(acceptance_checks(result)['registry_manifests'])
        result['recommendation']['eligibility']=[]
        self.assertFalse(acceptance_checks(result)['confirmation_evidence'])
