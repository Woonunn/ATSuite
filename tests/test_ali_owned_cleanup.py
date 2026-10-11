import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from atsuite.execution.cleanup import cleanup_owned_resource
from atsuite.recommender.identity import digest


class AliOwnedCleanupTests(unittest.TestCase):
    def make_case(self, owner=None):
        record={'id':'e','tenant':'t','project':'p','accounts':{'ali_fc':'123'},'approval':{'accounts':{'ali_fc':'123'}}}
        ownership=digest({'tenant':'t','project':'p','experiment':'e','candidate':'c'},length=64)
        resource={'provider':'ali_fc','region':'us-east-1','account_id':'123','physical_name':'at-owned-function',
                  'ownership':ownership,'tenant':'t','project':'p','experiment_id':'e','candidate_id':'c'}
        client=Mock()
        client.get_function_with_options.return_value.body=SimpleNamespace(tags=[SimpleNamespace(key='atsuite-ownership',value=owner or ownership)],function_arn='')
        client.list_triggers_with_options.side_effect=[
            SimpleNamespace(body=SimpleNamespace(triggers=[SimpleNamespace(trigger_name='first')],next_token='more')),
            SimpleNamespace(body=SimpleNamespace(triggers=[SimpleNamespace(trigger_name='second')],next_token=None))]
        return record,resource,client

    def test_owned_function_triggers_are_removed_before_function(self):
        record,resource,client=self.make_case()
        result=cleanup_owned_resource(resource,record,clients={'fc':client})
        self.assertEqual(result['cleanup_status'],'pending')
        names=[c[0] for c in client.mock_calls]
        self.assertEqual(names[-3:],['delete_trigger_with_options','delete_trigger_with_options','delete_function_with_options'])
        self.assertEqual(client.list_triggers_with_options.call_args_list[1].args[1].next_token,'more')

    def test_foreign_function_keeps_all_triggers(self):
        record,resource,client=self.make_case('foreign')
        with self.assertRaisesRegex(ValueError,'ownership_mismatch'):
            cleanup_owned_resource(resource,record,clients={'fc':client})
        client.list_triggers_with_options.assert_not_called()
        client.delete_trigger_with_options.assert_not_called()
        client.delete_function_with_options.assert_not_called()
