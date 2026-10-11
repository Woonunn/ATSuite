import copy
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch
from tools.recommender_budget_once import BOUNDS, LIFECYCLE, BudgetBackend, experiment_spec, validate_runtime_bound
from atsuite.recommender.protocol import ExperimentProtocol, Scenario


class BudgetOnceTests(unittest.TestCase):
    def market(self):
        return {'fx': {'rates': {'CNY': '6.7'}}, 'rate_cards': [
            {'region': 'us-east-1', 'currency': 'USD', 'tiers': [{'unit_price': rate}],
             'metadata': {'attributes': {'servicecode': 'AmazonBedrockAgentCore', 'usagetype': meter}}}
            for meter, rate in [('USE1-Runtime:Consumption-based:vCPU-v2', '.1276'),
                                ('USE1-Runtime:Consumption-based:Memory-v2', '.0169')]]}

    def test_exactly_four_paid_workflow_attempts_fit_budget(self):
        spec = experiment_spec()
        protocol = ExperimentProtocol('test', (Scenario('natural', 'natural'),),
                                      warmup_runs=spec.protocol.warmup_runs,
                                      exploration_runs=spec.protocol.exploration_runs,
                                      confirmation_runs=spec.protocol.confirmation_runs)
        self.assertEqual(len(protocol.schedule(list(spec.providers))), 4)
        self.assertEqual(protocol.schedule(list(spec.providers), phase='warmup'), [])
        self.assertEqual(protocol.schedule(list(spec.providers), phase='exploration'), [])
        self.assertEqual(4 * sum(map(Decimal, BOUNDS.values())), Decimal('4.48'))
        self.assertEqual(spec.budget.max_estimated_spend, 5)

    def test_price_increase_or_missing_runtime_price_blocks_execution(self):
        self.assertLess(validate_runtime_bound(self.market()), Decimal('.90'))
        expensive = copy.deepcopy(self.market())
        expensive['rate_cards'][0]['tiers'][0]['unit_price'] = '1'
        with self.assertRaisesRegex(ValueError, 'exceed'):
            validate_runtime_bound(expensive)
        missing = self.market()
        missing['rate_cards'].pop()
        with self.assertRaisesRegex(ValueError, 'coverage missing'):
            validate_runtime_bound(missing)

    def test_provider_lifetime_must_match_readback_before_measurement(self):
        from atsuite.aws.Agentcore import AWSAgentCore
        for observed, accepted in [(LIFECYCLE, True), ({'maxLifetime': 28800}, False)]:
            with self.subTest(observed=observed):
                client = Mock()
                client.get_agent_runtime.return_value = {'lifecycleConfiguration': observed}
                runtime = SimpleNamespace(agentcore_client=client, _runtime_id='runtime')

                def create(runtime, image):
                    runtime.agentcore_client.create_agent_runtime(tags={'atsuite-ownership': 'owner'})

                def deploy(*args):
                    AWSAgentCore._create_or_update_runtime(runtime, 'image')
                    return 'deployment'

                with patch.object(AWSAgentCore, '_create_or_update_runtime', create), patch('atsuite.execution.cloud_backend.CloudBackend.deploy', side_effect=deploy):
                    backend = BudgetBackend()
                    args = ({'provider': 'aws_agentcore'}, {}, {'ownership': 'owner'}, None)
                    if accepted:
                        self.assertEqual(backend.deploy(*args), 'deployment')
                    else:
                        with self.assertRaisesRegex(ValueError, 'readback mismatch'):
                            backend.deploy(*args)
                client.create_agent_runtime.assert_called_once_with(tags={'atsuite-ownership': 'owner'}, lifecycleConfiguration=LIFECYCLE)
