"""CLI migration contracts: legacy flags normalize into the shared service."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from atsuite.cli import recommend
from tests.test_recommender_candidates import make_config

class CliTests(unittest.TestCase):
    def test_subprocess_stdout_is_redirected(self):
        program="from atsuite.cli.recommend import execution_output_to_stderr\nimport os\nwith execution_output_to_stderr():\n print('python progress')\n os.write(1,b'child progress\\n')\nprint('{\"ok\":true}')"
        r=subprocess.run([sys.executable,'-c',program],capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(r.stdout),{'ok':True});self.assertIn('child progress',r.stderr);self.assertIn('python progress',r.stderr)
    def test_legacy_options_create_frozen_plan_without_cloud_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config=make_config(root/'bench');stdout=io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code=recommend.main(['--config',str(config),'--providers','aws_lambda,aws_agentcore','--runs','3','--warmup-runs','0',
                    '--objective','cost','--cloud','aws','--family','faas,session','--max-cost-usd','.01',
                    '--max-p95-ms','3000','--max-failure-rate','0','--max-memory-mb','1000','--output-dir',str(root/'out'),'--json'])
            self.assertEqual(code,0);payload=json.loads(stdout.getvalue())
            self.assertEqual(payload['status'],'planned');self.assertIsNone(payload['best_verified'])
            self.assertEqual(payload['spec']['objective']['type'],'min_cost');self.assertEqual(payload['spec']['objective']['max_memory_mb'],1000)
            self.assertEqual(payload['spec']['protocol']['confirmation_runs'],3);self.assertTrue((root/'out'/'plan.json').is_file())
    def test_queue_requires_actual_budget_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config=make_config(root/'bench')
            with contextlib.redirect_stdout(io.StringIO()):
                code=recommend.main(['--config',str(config),'--providers','aws_lambda','--output-dir',str(root/'out'),
                    '--approve-budget','--max-budget','5','--store',str(root/'jobs.sqlite')])
            self.assertEqual(code,0)
            from atsuite.execution.store import JobStore
            store=JobStore(root/'jobs.sqlite');record=store.get(store.queued()[0])
            self.assertEqual(record['state'],'APPROVED');self.assertEqual(record['approval']['budget']['max_estimated_spend'],5)
            self.assertEqual(record['approval']['plan_digest'],__import__('atsuite.recommender.service',fromlist=['plan_digest']).plan_digest(record['plan']))
            store.close()
    def test_validation_errors(self):
        for v in ('latency=.5,cost=.4','latency=.5,latency=.5','latency=nan','foo=1'):
            with self.assertRaises(Exception):recommend.parse_weights(v)
        with tempfile.TemporaryDirectory() as tmp:
            config=make_config(Path(tmp))
            for opts in (['--runs','1'],['--latency-stat','p50'],['--keep-resources']):
                with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as exc:recommend.main(['--config',str(config),*opts])
                self.assertEqual(exc.exception.code,2)
    def test_gateway_only_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config=make_config(root/'bench')
            with contextlib.redirect_stdout(io.StringIO()):recommend.main(['--config',str(config),'--include-provider','mcp_gateway','--output-dir',str(root/'out')])
            planned=json.loads((root/'out'/'plan.json').read_text())
            self.assertIn('mcp_gateway',planned['spec']['providers']);self.assertEqual(len(planned['spec']['providers']),7)
