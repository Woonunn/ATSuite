import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from experiments.exp1.data.prepare_e1b import PROVIDERS, sha
from experiments.exp1.runner import run_e1b_cloud as cloud
from experiments.exp1.runner.run_e1b_cloud import RENDERING_POLICY, pilot_rows, validate_pilots


class CloudPilotTests(unittest.TestCase):
    def test_failed_pilot_stops_further_paid_calls_and_providers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workload = root / 'workload'
            workload.mkdir()
            (workload / 'manifest.json').write_text('{}')
            base = root / 'benchmarks/DataSciBench/config/langgraph_experiment.json'
            base.parent.mkdir(parents=True)
            base.write_text(json.dumps({'max_parallel': 2}))
            policy = root / 'rendering.json'
            policy.write_text(json.dumps({'task_id': 'a', 'capture_events_sha256': 'capture',
                                          'evidence': [], 'approved_renderings': {}}))
            endpoints = root / 'url_results'
            endpoints.mkdir()
            (endpoints / 'aws_lambda.json').write_text(json.dumps({
                'provider': 'aws_lambda', 'targets': {'tool': {'endpoint': 'https://unused', 'family': 'faas'}},
            }))
            entries = [{'provider': p, 'config': p + '.json',
                        'image': {'source_sha256': {'/app/implementation.py': 'fixture'}}} for p in PROVIDERS]
            deployment = root / 'deployment.json'
            deployment.write_text(json.dumps({'results': entries}))
            manifest = {'config': {'scenarios': {'fixed': []}, 'window_roles': ['typical'],
                                   'window_duration_ms': 600000, 'seed': 11, 'queue_capacity': 1000},
                        'request_table_sha256': 'fixture', 'groups': [{'provider_order': PROVIDERS}],
                        'tasks': {t: {'capture_files': {'events.jsonl': 'capture'}, 'capture': 'unused'} for t in ['a', 'b']}}
            rows = [{'arrival_id': t, 'task_id': t, 'relative_arrival_ms': '0', 'trace_sha256': 'capture',
                     'original_timestamp_utc': '2026-10-29T00:00:00Z', 'source_partition': 'fixture',
                     'source_type': 'fixture', 'window_id': 'fixture'} for t in ['a', 'b']]
            replay = AsyncMock(side_effect=RuntimeError('fixture failure'))
            with (patch.object(cloud, 'ROOT', root), patch.object(cloud, 'RENDERING_POLICY', policy),
                  patch.object(cloud, 'load', return_value=(manifest, rows)),
                  patch.object(cloud, 'validate_deployment'), patch.object(cloud, 'collect_local'),
                  patch.object(cloud, 'replay', replay)):
                with self.assertRaisesRegex(RuntimeError, 'Cloud replay failed'):
                    asyncio.run(cloud.execute(workload, deployment,
                        {'budget_policy': 'test', 'tool_rpc_timeout_s': 240}, root / 'pilot', pilot=True))
            self.assertEqual(replay.await_count, 1)
            matrix = json.loads((root / 'pilot/matrix.json').read_text())
            self.assertEqual(matrix['status'], 'failed')
            self.assertTrue(all(g['status'] == 'not_started' for g in matrix['groups'][1:]))

    def test_pilot_preserves_first_original_arrival_per_task(self):
        rows = [{'task_id': task, 'arrival_id': str(i), 'relative_arrival_ms': str(i * 1700)}
                for i, task in enumerate(['a', 'a', 'b', 'c', 'b'])]
        self.assertEqual(pilot_rows(rows), [rows[0], rows[2], rows[3]])

    def test_formal_admission_rejects_missing_failure_and_lifecycle_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workload = root / 'workload'
            workload.mkdir()
            (workload / 'manifest.json').write_text('{}')
            deployment = root / 'deployment.json'
            deployment.write_text('{}')
            pilots = root / 'pilots'
            for provider in PROVIDERS:
                run = pilots / provider
                events_dir = run / 'requests' / 'arrival'
                events_dir.mkdir(parents=True)
                (run / 'run.json').write_text(json.dumps({
                    'pilot': True, 'provider': provider,
                    'workload_manifest_sha256': sha(workload / 'manifest.json'),
                    'deployment_manifest_sha256': sha(deployment),
                    'rendering_policy_sha256': sha(RENDERING_POLICY),
                }))
                (run / 'request_metrics.jsonl').write_text(json.dumps({
                    'task_id': 'a', 'arrival_id': 'arrival', 'status': 'succeeded',
                }) + '\n')
                events = [{'kind': 'tool_session_open', 'uid': 'session'}, {
                    'kind': 'tool', 'tool_name': 'execute_analysis', 'result': {'state_lifecycle': {
                        'canonical_restore_performed': provider in {'aws_lambda', 'ali_fc'},
                        'canonical_save_performed': False,
                    }},
                }, {'kind': 'tool', 'tool_name': 'merge_artifacts', 'result': {
                    'status': 'ok', 'kernel_execution': {'state_lifecycle': {
                        'canonical_restore_performed': provider in {'aws_lambda', 'ali_fc'},
                        'canonical_save_performed': provider in {'aws_lambda', 'ali_fc'},
                    }},
                }}]
                (events_dir / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
            self.assertEqual(len(validate_pilots(pilots, workload, deployment, ['a'])), 12)
            with self.assertRaisesRegex(ValueError, 'another workload or deployment'):
                validate_pilots(pilots, workload, deployment, ['a'], {'aws_lambda': {'a': {'capture': 'new'}}})
            candidate = root / 'candidate'
            candidate.mkdir()
            (candidate / 'events.jsonl').write_text(json.dumps({'kind': 'tool_session_open', 'uid': 'calibration'}) + '\n')
            references = {'aws_lambda': {'a': {'capture': str(candidate)}}}
            run_path = pilots / 'aws_lambda/run.json'
            run_info = json.loads(run_path.read_text())
            run_path.write_text(json.dumps({**run_info, 'environment_references': references['aws_lambda']}))
            event_path = pilots / 'aws_lambda/requests/arrival/events.jsonl'
            pilot_events = event_path.read_text()
            start = {'kind': 'run_start', 'mode': 'environment_reference', 'source_capture': str(candidate)}
            event_path.write_text(pilot_events + json.dumps(start) + '\n')
            with self.assertRaisesRegex(ValueError, 'strict replay in a fresh'):
                validate_pilots(pilots, workload, deployment, ['a'], references)
            start['mode'] = 'replay'
            event_path.write_text(pilot_events + json.dumps(start) + '\n')
            self.assertEqual(len(validate_pilots(pilots, workload, deployment, ['a'], references)), 12)
            (candidate / 'events.jsonl').write_text(json.dumps({'kind': 'tool_session_open', 'uid': 'session'}) + '\n')
            with self.assertRaisesRegex(ValueError, 'strict replay in a fresh'):
                validate_pilots(pilots, workload, deployment, ['a'], references)
            run_path.write_text(json.dumps(run_info))
            event_path.write_text(pilot_events)
            provider_entry = {'provider': 'aws_lambda', 'image': 'unchanged'}
            run_path.write_text(json.dumps({**run_info, 'deployment': provider_entry}))
            deployment.write_text(json.dumps({'results': [provider_entry], 'other_provider_changed': True}))
            self.assertEqual(len(validate_pilots(pilots, workload, deployment, ['a'], providers=['aws_lambda'])), 3)
            deployment.write_text(json.dumps({'results': [{**provider_entry, 'image': 'changed'}]}))
            with self.assertRaisesRegex(ValueError, 'another workload or deployment'):
                validate_pilots(pilots, workload, deployment, ['a'], providers=['aws_lambda'])
            deployment.write_text('{}')
            run_path.write_text(json.dumps(run_info))
            with self.assertRaises(ValueError):
                validate_pilots(pilots, workload, deployment, ['a', 'b'])
            path = pilots / 'aws_agentcore/requests/arrival/events.jsonl'
            events = [json.loads(line) for line in path.read_text().splitlines()]
            events[1]['result']['state_lifecycle']['canonical_save_performed'] = True
            path.write_text(''.join(json.dumps(e) + '\n' for e in events))
            with self.assertRaisesRegex(ValueError, 'MCP Pilot'):
                validate_pilots(pilots, workload, deployment, ['a'])
            path = pilots / 'aws_lambda/request_metrics.jsonl'
            row = json.loads(path.read_text())
            row['status'] = 'failed'
            path.write_text(json.dumps(row) + '\n')
            with self.assertRaisesRegex(ValueError, 'Every task'):
                validate_pilots(pilots, workload, deployment, ['a'])


if __name__ == '__main__':
    unittest.main()
