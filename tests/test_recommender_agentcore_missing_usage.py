import json
import unittest
from datetime import datetime,timezone
from types import SimpleNamespace
from atsuite.aws.CloudWatch import AWSCloudWatch

class AgentCoreMissingUsage(unittest.TestCase):
    def client(self,metrics,groups=('group',)):
        client=AWSCloudWatch.__new__(AWSCloudWatch);client.region='us-east-1'
        client.discover_agentcore_usage_log_groups=lambda runtime:list(groups)
        message={'attributes':{'session.id':'s'},'metrics':metrics}
        client.logs=SimpleNamespace(filter_log_events=lambda **kw:{'events':[{'eventId':'meter-1','message':json.dumps(message)}]})
        return client
    def test_missing_component_is_not_observed_zero(self):
        now=datetime.now(timezone.utc)
        value=self.client({'agent.runtime.vcpu.hours.used':.1}).get_agentcore_usage_from_logs('r','s',now,now)
        self.assertIsNone(value.get('memory_gb_hours'))
        self.assertEqual(value['vcpu_hours'],.1)
    def test_missing_log_group_has_no_billable_zero(self):
        now=datetime.now(timezone.utc);value=self.client({},groups=()).get_agentcore_usage_from_logs('r','s',now,now)
        self.assertIsNone(value.get('vcpu_hours'));self.assertIsNone(value.get('memory_gb_hours'))
        self.assertTrue(value['missing_log_group'])
    def test_real_zero_component_retains_observed_identity(self):
        now=datetime.now(timezone.utc);value=self.client({'agent.runtime.vcpu.hours.used':0,'agent.runtime.memory.gb_hours.used':.2}).get_agentcore_usage_from_logs('r','s',now,now)
        self.assertEqual(value['vcpu_hours'],0);self.assertEqual(value['memory_gb_hours'],.2)
        self.assertEqual(value['provider_meter_ids'],['meter-1'])
