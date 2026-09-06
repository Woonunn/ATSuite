import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from atsuite.cli import recommend
from atsuite.recommender.engine import RecommendationEngine
from tests.test_recommender_candidates import make_config
from tests.test_recommender_engine import FakeExperimentRunner


class CliTests(unittest.TestCase):
    def test_subprocess_stdout_is_redirected(self):
        program = """from atsuite.cli.recommend import execution_output_to_stderr
import os
with execution_output_to_stderr():
    print('python progress')
    os.write(1, b'child progress\\n')
print('{"ok": true}')
"""
        result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"ok": True})
        self.assertIn("child progress", result.stderr)
        self.assertIn("python progress", result.stderr)

    def test_json_stdout_and_full_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            runner = FakeExperimentRunner()
            engine = RecommendationEngine(runner, output_dir=Path(tmp) / "out")
            fx = Path(tmp) / "fx.json"
            fx.write_text(json.dumps({"base_currency": "USD", "rates": {"CNY": .14}}))
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(recommend, "RecommendationEngine", return_value=engine), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = recommend.main([
                    "--config", str(config), "--providers", "aws_lambda,aws_agentcore", "--runs", "3",
                    "--warmup-runs", "0", "--objective", "cost", "--latency-stat", "p50",
                    "--fx-config", str(fx), "--cloud", "aws", "--family", "faas,session", "--max-cost-usd", ".01",
                    "--max-p95-ms", "3000", "--max-failure-rate", "0", "--max-memory-mb", "1000",
                    "--keep-resources", "--output-dir", str(Path(tmp) / "out"), "--json",
                ])
            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["recommendation"]["provider"], "aws_lambda")
            self.assertEqual(payload["experiment"]["runs"], 3)
            self.assertEqual(payload["pricing"]["fx_rates"]["CNY"], .14)

    def test_readable_output_and_unavailable_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            engine = RecommendationEngine(FakeExperimentRunner(), output_dir=Path(tmp) / "out")
            stdout = io.StringIO()
            with mock.patch.object(recommend, "RecommendationEngine", return_value=engine), contextlib.redirect_stdout(stdout):
                code = recommend.main(["--config", str(config), "--providers", "aws_lambda"])
            self.assertEqual(code, 1)
            self.assertIn("Recommendation unavailable", stdout.getvalue())
            self.assertIn("configured shape", stdout.getvalue())
            result = engine.recommend(config, providers=["aws_lambda", "aws_agentcore"])
            output = recommend.format_recommendation(result)
            for text in ("Best FaaS", "Best Session-MCP", "Confidence", "CV", "Report:", "P95 ms"):
                self.assertIn(text, output)

    def test_validation_errors(self):
        for value in ("latency=.5,cost=.4", "latency=.5,latency=.5", "latency=nan", "foo=1"):
            with self.assertRaises(Exception):
                recommend.parse_weights(value)
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                recommend.main(["--config", str(config), "--runs", "1"])
            self.assertEqual(exc.exception.code, 2)

    def test_default_providers_include_gateway_only_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            runner = FakeExperimentRunner()
            engine = RecommendationEngine(runner, output_dir=Path(tmp) / "out")
            with mock.patch.object(recommend, "RecommendationEngine", return_value=engine), contextlib.redirect_stdout(io.StringIO()):
                recommend.main(["--config", str(config), "--include-provider", "mcp_gateway"])
            self.assertEqual(len(runner.calls), 7)
            self.assertIn("mcp_gateway", runner.calls)
