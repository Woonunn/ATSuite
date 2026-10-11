import unittest
from unittest.mock import Mock, patch

from atsuite.mcp.mcp import AgentCoreMCPClient
from atsuite.runtime import MCPRuntimeAdapter


class AgentCoreSessionCleanupTests(unittest.TestCase):
    def test_stops_only_the_finished_run_and_preserves_other_sessions(self):
        adapter = MCPRuntimeAdapter('aws_agentcore')
        adapter.targets = {'counter': Mock()}
        finished, other = Mock(), Mock()
        adapter._clients = {'counter:finished': finished, 'counter:other': other}
        adapter.cleanup_run('finished')
        finished.close.assert_called_once_with()
        other.close.assert_not_called()
        self.assertEqual(adapter._clients, {'counter:other': other})

    def test_stops_the_encoded_runtime_and_qualifier_once(self):
        client = AgentCoreMCPClient.__new__(AgentCoreMCPClient)
        client.url = 'https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A123%3Aruntime%2Ftest/invocations?qualifier=prod'
        client.region = 'us-east-1'
        client.session_id = '12345678-1234-1234-1234-123456789012'
        with patch('boto3.client') as factory:
            client.close()
            client.close()
        factory.assert_called_once_with('bedrock-agentcore', region_name='us-east-1')
        factory.return_value.stop_runtime_session.assert_called_once_with(
            agentRuntimeArn='arn:aws:bedrock-agentcore:us-east-1:123:runtime/test',
            runtimeSessionId='12345678-1234-1234-1234-123456789012', qualifier='prod',
        )

    def test_failed_stop_keeps_the_session_and_surfaces_the_failure(self):
        client = AgentCoreMCPClient.__new__(AgentCoreMCPClient)
        client.url = 'https://example.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A123%3Aruntime%2Ftest/invocations'
        client.region = 'us-east-1'
        client.session_id = 'session'
        with patch('boto3.client') as factory:
            factory.return_value.stop_runtime_session.side_effect = RuntimeError('denied')
            with self.assertRaisesRegex(RuntimeError, 'denied'):
                client.close()
        self.assertEqual(client.session_id, 'session')
