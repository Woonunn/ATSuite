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
    def test_nonfinite_summary_does_not_break_json_export(self):
        from atsuite.analysis.model import AnalysisReport
        from atsuite.recommender import experiment
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            lifecycle = CandidateLifecycle(generate_candidates(config, ["aws_lambda"])[0], Path(tmp))
            lifecycle.url_map_path = Path(tmp) / "urls.json"
            lifecycle.url_map_path.write_text(json.dumps({"targets": {"tool": "https://example.test"}}))
            report = AnalysisReport(2, {}, {"total_price": float("nan"), "metric_availability": {"total_price": "available"}}, {}, [], [], [])
            with mock.patch.object(experiment, "run_trace", return_value={"status": "success", "summary": report.summary}), \
                 mock.patch("atsuite.analyzer.Analyzer.get_report", return_value=report):
                record = lifecycle.run("u", "measured")
            self.assertIsNone(record["summary"]["total_price"])
            self.assertEqual(record["summary"]["metric_availability"]["total_price"], "unavailable")
            self.assertTrue((Path(tmp) / "u" / "run.json").is_file())

    def test_clients_receive_configured_timeout(self):
        from atsuite.runtime import FunctionRuntimeAdapter, MCPRuntimeAdapter, InvocationRequest
        for adapter in (FunctionRuntimeAdapter("aws_lambda"), MCPRuntimeAdapter("gcp_mcp")):
            adapter.connect({"targets": {"tool": "https://example.test"}})
            client = mock.Mock()
            client.invoke.return_value = (("request", False, {"value": 1})
                                          if isinstance(adapter, FunctionRuntimeAdapter)
                                          else ("request", {"value": 1}))
            with mock.patch.object(adapter, "_client", return_value=client):
                result = adapter.invoke(InvocationRequest("tool", "run", {}, "u", timeout=7))
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.result, {"value": 1})
            self.assertEqual(client.timeout, 7)

    def test_correctness_hook_uses_captured_business_output(self):
        from atsuite.recommender.correctness import validate_outputs
        actual = {"call-1": {"output": {"value": 1, "request_id": "ignored"}}}
        self.assertEqual(
            validate_outputs(actual, {"call-1": {"value": 1}})["status"],
            "passed",
        )
        self.assertEqual(
            validate_outputs(actual, {"call-1": {"value": 2}})["status"],
            "failed",
        )

    def test_clients_capture_function_and_mcp_business_results(self):
        from atsuite.faas.function import FunctionClient
        from atsuite.mcp.mcp import _extract_mcp_tool_result

        response = mock.Mock()
        response.json.return_value = {"result": '{"value": 1}'}
        response.headers = {"x-amzn-requestid": "provider-request", "X-Tool-Stateful": "true"}
        response.raise_for_status.return_value = None
        client = FunctionClient("https://example.test")
        with mock.patch.object(client.session, "post", return_value=response):
            self.assertEqual(client.invoke("tool", {}), ("provider-request", True, {"value": 1}))

        self.assertEqual(
            _extract_mcp_tool_result({"result": {"content": [{"type": "text", "text": '{"value": 2}'}]}}),
            {"value": 2},
        )
        self.assertEqual(
            _extract_mcp_tool_result({"result": {"structuredContent": {"value": 3}}}),
            {"value": 3},
        )

    def test_build_and_deploy_use_python_apis_and_snapshot_map(self):
        from atsuite.recommender import experiment
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            lifecycle = CandidateLifecycle(generate_candidates(config, ["aws_lambda"])[0], Path(tmp))
            url_map = Path(tmp) / "urls.json"
            url_map.write_text(json.dumps({"targets": {"tool": {"endpoint": "https://example.test"}}}))
            with mock.patch.object(experiment, "build_resolved_targets") as build, mock.patch.object(experiment, "deploy", return_value=url_map) as deploy:
                lifecycle.build()
                lifecycle.deploy()
            build.assert_called_once_with(config.resolve(), "aws_lambda", str(Path(tmp) / "build"))
            deploy.assert_called_once_with(config.resolve(), "aws_lambda")
            self.assertEqual(json.loads(lifecycle.url_map_path.read_text()), json.loads(url_map.read_text()))
            url_map.write_text(json.dumps({"targets": {"tool": {"endpoint": ""}}}))
            with mock.patch.object(experiment, "deploy", return_value=url_map), self.assertRaisesRegex(RuntimeError, "no endpoints"):
                lifecycle.deploy()
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
        client.timeout = 7
        with mock.patch.object(client.session, "post", return_value=response) as post:
            with self.assertRaisesRegex(RuntimeError, "isError"):
                client.invoke("tool", {})
        self.assertEqual(post.call_args.kwargs["timeout"], 7)

    def test_analysis_failure_is_not_a_workflow_failure(self):
        class Runtime:
            def connect(self, _): pass
            def open_session(self, target, uid): return RuntimeSession(uid, target.target_id)
            def invoke(self, _): return InvocationResult(client_elapsed_ms=10)
            def cleanup_run(self, _): pass

        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            lifecycle = CandidateLifecycle(generate_candidates(config, ["aws_lambda"])[0], Path(tmp))
            lifecycle.url_map_path = Path(tmp) / "urls.json"
            lifecycle.url_map_path.write_text(json.dumps({"targets": {"tool": "https://example.test"}}))
            with mock.patch.object(invoker, "create_runtime_adapter", return_value=Runtime()), \
                 mock.patch("atsuite.analyzer.Analyzer.end", side_effect=RuntimeError("collector failed")), \
                 contextlib.redirect_stdout(io.StringIO()):
                record = lifecycle.run("u1", "measured")
            self.assertEqual(record["status"], "success")
            self.assertTrue(any(d["kind"] == "analysis_failed" for d in record["diagnostics"]))
            self.assertGreater(record["summary"]["run_user_e2e_ms"], 0)

    def test_partial_coverage_and_agentcore_missing_usage(self):
        from atsuite.analysis.model import CostLineItem
        invocations = [InvocationObservation(i, "tool", "tool", "tool", "session", "run", str(i), "u", provider_session_id=f"s{i}") for i in (1, 2)]
        nodes = {i: NodeObservation(i, "tool", "mcp", runtime_config={"memory": 512}) for i in (1, 2)}
        context = RunContext("u", "aws_agentcore", "aws_agentcore_cloudwatch", "Bench", "trace", "session", "config", 1, 2)
        metrics = [_metric_from_invocation("aws_agentcore", "cloudwatch", call, nodes[call.node_id], {"memory_usage_mb": 100} if call.node_id == 1 else {}) for call in invocations]
        report = ReportAggregator().aggregate(context, nodes=nodes, invocations=invocations, metrics=metrics,
                                             costs=[CostLineItem("storage", "aws_agentcore", .01, "USD")], diagnostics=[], evidence=[])
        self.assertEqual(report.summary["metric_availability"]["avg_memory_usage_mb"], "partial")
        self.assertEqual(report.summary["metric_availability"]["total_price"], "unavailable")
