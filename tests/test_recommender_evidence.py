import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from atsuite import invoker
from atsuite.analysis.aggregation import ReportAggregator
from atsuite.analysis.collectors import NoopCollector, _metric_from_invocation
from atsuite.analysis.model import InvocationObservation, NodeObservation, RunContext
from atsuite.recommender.candidates import generate_candidates
from atsuite.recommender.experiment import CandidateLifecycle
from atsuite.runtime import InvocationResult, RuntimeSession
from tests.test_recommender_candidates import make_config


class EvidenceTests(unittest.TestCase):
    def test_fallbacks_are_unavailable_and_observed_zero_is_available(self):
        invocation = InvocationObservation(1, "tool", "tool", "tool", "faas", "run", "call", "u")
        node = NodeObservation(1, "tool", "function", runtime_config={"memory": 512})
        context = RunContext("u", "aws_lambda", "aws_lambda_cloudwatch", "Bench", "trace", "faas", "config", 1, 2)
        for fields, expected in (({}, "unavailable"), ({"is_cold_start": "false", "state_sync_overhead_ms": 0, "memory_usage_mb": 100}, "available")):
            metric = _metric_from_invocation("aws_lambda", "runtime", invocation, node, fields)
            report = ReportAggregator().aggregate(context, nodes={1: node}, invocations=[invocation], metrics=[metric], costs=[], diagnostics=[], evidence=[])
            statuses = report.summary["metric_availability"]
            self.assertEqual(statuses["cold_start_total"], expected)
            self.assertEqual(statuses["avg_memory_usage_mb"], expected)
            self.assertEqual(statuses["total_state_sync_overhead_ms"], expected)
            self.assertEqual(statuses["total_price"], "unavailable")

    def test_real_replay_api_exports_failed_run_and_restores_stdout(self):
        class Runtime:
            def connect(self, _): pass
            def open_session(self, target, uid): return RuntimeSession(uid, target.target_id)
            def invoke(self, _): return InvocationResult(status="error", error="provider error")
            def cleanup_run(self, _): pass

        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            candidate = generate_candidates(config, ["aws_lambda"])[0]
            lifecycle = CandidateLifecycle(candidate, Path(tmp))
            lifecycle.url_map_path = Path(tmp) / "urls.json"
            lifecycle.url_map_path.write_text(json.dumps({"provider": "aws_lambda", "targets": {"tool": "https://example.test"}}))
            with mock.patch.object(invoker, "create_runtime_adapter", return_value=Runtime()), \
                 mock.patch.object(invoker, "cleaner", return_value=0), \
                 mock.patch("atsuite.analysis.api.create_collector", return_value=NoopCollector()), \
                 contextlib.redirect_stdout(io.StringIO()):
                before = sys.stdout
                record = lifecycle.run("u1", "measured")
                self.assertIs(sys.stdout, before)
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["summary"]["failed_invocation_count"], 1)
                self.assertTrue(Path(record["report_path"]).is_file())
                with mock.patch.object(invoker, "create_runtime_adapter", side_effect=RuntimeError("connect failed")):
                    failed = lifecycle.run("u2", "measured")
                self.assertEqual(failed["status"], "failed")
                self.assertIs(sys.stdout, before)

    def test_tool_errors_propagate_through_clients(self):
        from atsuite.faas.function import FunctionClient
        from atsuite.mcp.mcp import _raise_mcp_tool_error
        payload = {"result": {"isError": True, "content": [{"text": "bad input"}]}}
        with self.assertRaisesRegex(RuntimeError, "bad input"):
            _raise_mcp_tool_error(payload, client_name="MCP", tool_name="tool")
        client = FunctionClient("https://example.test")
        response = mock.Mock()
        response.json.return_value = payload
        with mock.patch.object(client.session, "post", return_value=response), self.assertRaisesRegex(RuntimeError, "isError"):
            client.invoke("tool", {})
