import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from atsuite.runtime import RuntimeSession
from atsuite_sdk.response import ToolResponse
from benchmarks.DataSciBench.langgraph_agent.tool_client import ToolClient, DivergenceError
from benchmarks.DataSciBench.langgraph_agent.trace_replayer import ReplayModel
from experiments.exp1.runner.e1b_references import seal_reference, load_references
from experiments.exp1.data.prepare_e1b import sha


class ReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_calibration_keeps_program_and_control_gates_and_records_actual_llm_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            capture = Path(temp)
            tool = {'kind': 'tool', 'key': 'train', 'tool_name': 'execute_analysis',
                    'args': {'code': 'frozen', 'seed': 0},
                    'result': {'status': 'ok', 'state_signature': 'old', 'checks': {'loss': 0.02}}}
            llm = {'kind': 'llm', 'phase': 'REVIEW', 'iteration': 0, 'duration_s': 0,
                   'request': {'messages': [{'role': 'user', 'content': 'old'}]},
                   'decision': {'verdict': 'complete'}}
            (capture / 'events.jsonl').write_text(json.dumps(tool) + '\n' + json.dumps(llm) + '\n')
            (capture / 'result.json').write_text('{"status":"complete"}')
            recorder = MagicMock()
            adapter = MagicMock()
            adapter.targets = {'t': object()}
            adapter.open_session.return_value = RuntimeSession(uid='new-session', target_id='t')
            actual = {'status': 'ok', 'state_signature': 'new', 'checks': {'loss': 0.021}}
            adapter.invoke.return_value = ToolResponse(result=actual)
            with patch('benchmarks.DataSciBench.langgraph_agent.tool_client.create_runtime_adapter', return_value=adapter):
                client = ToolClient({'provider': 'unit', 'family': 'faas', 'target_id': 't', 'endpoint': 'local'},
                                    recorder, capture, calibration=True)
                with self.assertRaises(DivergenceError):
                    await client.call('execute_analysis', {'code': 'modified', 'seed': 0}, 'train')
                adapter.invoke.assert_not_called()
                self.assertEqual(await client.call('execute_analysis', tool['args'], 'train'), actual)
                adapter.invoke.return_value = ToolResponse(result={'status': 'error', 'error_type': 'Invalid'})
                with self.assertRaises(DivergenceError):
                    await client.call('execute_analysis', tool['args'], 'train')
                client.close()
            messages = [{'role': 'user', 'content': 'new actual results'}]
            model = ReplayModel(capture, recorder, calibration=True)
            self.assertEqual(await model.decide('REVIEW', 0, messages), llm['decision'])
            emitted = next(c for c in recorder.emit.call_args_list if c.args == ('llm',))
            self.assertEqual(emitted.kwargs['request']['messages'], messages)
            strict = ReplayModel(capture, recorder)
            with self.assertRaisesRegex(ValueError, 'inputs diverged'):
                await strict.decide('REVIEW', 0, messages)

    async def test_reference_requires_independent_validation_and_frozen_decisions_and_sealed_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original, candidate = root / 'original', root / 'candidate'
            original.mkdir(); candidate.mkdir()
            decision = {'kind': 'llm', 'phase': 'PLAN', 'iteration': 0, 'decision': {'steps': []}, 'duration_s': 0}
            (original / 'events.jsonl').write_text(json.dumps(decision) + '\n')
            start = {'kind': 'run_start', 'mode': 'environment_reference', 'task_id': 't',
                     'config': {'tool': {'provider': 'aws_lambda'}}, 'source_capture': str(original)}
            (candidate / 'events.jsonl').write_text(json.dumps(start) + '\n' + json.dumps(decision) + '\n')
            result = candidate / 'result.json'
            result.write_text('{"status":"complete","validation":{"status":"error"}}')
            with self.assertRaisesRegex(ValueError, 'independent'):
                seal_reference(candidate, original, 't', 'aws_lambda')
            result.write_text('{"status":"complete","validation":{"status":"ok"}}')
            entry = seal_reference(candidate, original, 't', 'aws_lambda')
            deployment = root / 'deployment.json'; deployment.write_text('{}')
            refs = root / 'references.json'
            refs.write_text(json.dumps({'deployment_manifest_sha256': sha(deployment),
                                        'providers': {'aws_lambda': {'t': entry}}}))
            manifest = {'tasks': {'t': {'capture': str(original)}}}
            self.assertEqual(load_references(refs, manifest, deployment)['aws_lambda']['t'], entry)
            references = json.loads(refs.read_text())
            provider_entry = {'provider': 'aws_lambda', 'image': 'sealed'}
            references['deployments'] = {'aws_lambda': provider_entry}
            refs.write_text(json.dumps(references))
            deployment.write_text(json.dumps({'results': [provider_entry], 'other_provider_changed': True}))
            self.assertEqual(load_references(refs, manifest, deployment)['aws_lambda']['t'], entry)
            deployment.write_text(json.dumps({'results': [{**provider_entry, 'image': 'changed'}]}))
            with self.assertRaisesRegex(ValueError, 'provider deployment changed'):
                load_references(refs, manifest, deployment)
            deployment.write_text(json.dumps({'results': [provider_entry]}))
            result.write_text('{"status":"complete","validation":{"status":"ok","changed":true}}')
            with self.assertRaisesRegex(ValueError, 'seal changed'):
                load_references(refs, manifest, deployment)
            decision['decision'] = {'steps': ['changed']}
            (candidate / 'events.jsonl').write_text(json.dumps(start) + '\n' + json.dumps(decision) + '\n')
            with self.assertRaisesRegex(ValueError, 'frozen decisions'):
                seal_reference(candidate, original, 't', 'aws_lambda')


if __name__ == '__main__':
    unittest.main()
