"""Phase 0 contracts: real local transports, concurrent sessions and failure evidence."""
import asyncio
import importlib
import json
import os
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from atsuite.runtime import FunctionRuntimeAdapter, InvocationRequest, MCPRuntimeAdapter
from atsuite_sdk.abstract import UnifiedAgentTool
from atsuite_sdk.events import EventLog
from atsuite_sdk import state
from atsuite_sdk.response import mcp_response
from phase0_environment import LocalToolEnvironment

ROOT = Path(__file__).resolve().parents[1]
MODULE = "benchmarks.DataSciBench.nodes.DataSci.implementation"


class StateIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"ATSUITE_SESSION_ROOT": self.tmp.name, "PROVIDER": "", "ATSUITE_RUNTIME": "function"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.runtime = state.StateRuntime()
        self.patch = mock.patch.object(state, "_RUNTIME", self.runtime)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.module = importlib.import_module(MODULE)
        self.workspace = self.runtime.register_object("datasci_workspace", self.module.DataSciWorkspace(), MODULE, session_scoped=True)
        self.workspace_patch = mock.patch.object(self.module, "datasci_workspace", self.workspace)
        self.workspace_patch.start()
        self.addCleanup(self.workspace_patch.stop)

    def test_faas_sixteen_sessions_keep_variables_files_and_streams_private(self):
        cwd, stdout = Path.cwd(), sys.stdout
        runtime_name = "function"
        gate = threading.Barrier(16)
        def session(index):
            uid = f"{runtime_name}-{index}"
            gate.wait(timeout=10)
            first = self.module.datasci_run_code.execute_from_dict({"uid": uid, "code_str":
                f"import os\nsaved_cwd = os.getcwd()\nvalues = [{index}, {index + 1}, {index + 2}]\n"
                "answer = sum(x*x for x in values)\n"
                f"open('input.txt', 'w').write(str({index}))\nprint(answer)"})
            second = self.module.datasci_run_code.execute_from_dict({"uid": uid, "code_str":
                "assert os.getcwd() == saved_cwd\nprint(values, answer, open('input.txt').read())"})
            expected = sum(x*x for x in [index, index+1, index+2])
            self.assertEqual(first, f"{expected}\n")
            self.assertEqual(second, f"[{index}, {index+1}, {index+2}] {expected} {index}\n")
            snapshot = self.runtime._get_snapshot(uid)
            self.assertEqual(snapshot["vars"][MODULE]["datasci_workspace"]["variables"]["values"], [index, index+1, index+2])
            self.assertIn("input.txt", snapshot["files"])
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(session, range(16)))
        self.assertEqual(Path.cwd(), cwd)
        self.assertIs(sys.stdout, stdout)

    def test_readers_overlap_and_do_not_commit_and_writers_serialize(self):
        counter = self.runtime.register_object("counter", SimpleNamespace(value=0), __name__, session_scoped=True)
        def increment():
            counter.value += 1
            return counter.value
        write = UnifiedAgentTool(increment, access="rw")
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: write.execute_from_dict({"uid": "shared"}), range(16)))
        self.assertEqual(sorted(results), list(range(1, 17)))
        gate = threading.Barrier(16)
        def read():
            value = counter.value
            counter.value = -1
            gate.wait(timeout=10)
            return value
        reader = UnifiedAgentTool(read, access="r")
        with mock.patch.object(self.runtime, "save_after_tool", wraps=self.runtime.save_after_tool) as save:
            with ThreadPoolExecutor(max_workers=16) as pool:
                values = list(pool.map(lambda _: reader.execute_from_dict({"uid": "shared"}), range(16)))
            self.assertEqual(values, [16]*16)
            save.assert_not_called()
        self.assertEqual(write.execute_from_dict({"uid": "shared"}), 17)
        stateless = UnifiedAgentTool(lambda: "ok", access="stateless")
        with mock.patch.object(self.runtime, "load_for_tool") as load, mock.patch.object(self.runtime, "save_after_tool") as save:
            self.assertEqual(stateless.execute_from_dict({"uid": "shared"}), "ok")
            load.assert_not_called()
            save.assert_not_called()
        write_only = UnifiedAgentTool(lambda: setattr(counter, "value", 99), access="w")
        write_only.execute_from_dict({"uid": "shared"})
        self.assertEqual(write.execute_from_dict({"uid": "shared"}), 100)
        with self.assertRaises(ValueError):
            UnifiedAgentTool(lambda: None, access="invalid")

    def test_failure_and_timeout_do_not_commit_partial_state(self):
        tool = self.module.datasci_run_code
        tool.execute_from_dict({"uid": "failed", "code_str": "x = 7"})
        with self.assertRaisesRegex(RuntimeError, "ZeroDivisionError"):
            tool.execute_from_dict({"uid": "failed", "code_str": "x = 999\nopen('bad', 'w').write('bad')\n1/0"})
        with mock.patch.dict(os.environ, {"ATSUITE_DATASCI_TIMEOUT": "0.05"}):
            with self.assertRaises(TimeoutError):
                tool.execute_from_dict({"uid": "failed", "code_str": "while True: pass"})
        output = tool.execute_from_dict({"uid": "failed", "code_str": "import os\nprint(x, os.path.exists('bad'))"})
        self.assertEqual(output, "7 False\n")

    def test_uid_collisions_path_escape_and_corrupt_snapshot_are_rejected(self):
        self.assertNotEqual(state._safe_uid("a/b"), state._safe_uid("a_b"))
        self.assertEqual(state._safe_uid(state._safe_uid("a/b")), state._safe_uid("a/b"))
        for path in ("../default-other/file", "/tmp/file"):
            with self.assertRaises(ValueError):
                self.runtime.session_path(path)
        target = self.runtime.root / ".snapshots" / self.runtime._snapshot_key("broken")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("broken", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            self.module.datasci_run_code.execute_from_dict({"uid": "broken", "code_str": "print(1)"})


class TransportTests(unittest.TestCase):
    """Real local HTTP; each MCP Session has a distinct server process."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.servers, cls.adapters = [], []
        try:
            for family, adapter_class in (("function", FunctionRuntimeAdapter), ("mcp", MCPRuntimeAdapter)):
                server = LocalToolEnvironment(Path(cls.tmp.name)/family, family, "session")
                cls.servers.append(server)
                adapter = adapter_class("test")
                adapter.connect({"targets": {"datasci": server.url}})
                cls.adapters.append(adapter)
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        for adapter in cls.adapters:
            for client in list(adapter._clients.values()):
                if hasattr(client, "close"):
                    client.close()
                elif hasattr(client, "session"):
                    client.session.close()
        for server in cls.servers:
            server.close()
        artifact_dir = os.getenv("ATSUITE_TEST_ARTIFACT_DIR")
        if artifact_dir:
            shutil.copytree(cls.tmp.name, Path(artifact_dir)/"transports", dirs_exist_ok=True)
        cls.tmp.cleanup()

    def test_real_faas_and_mcp_return_equal_outputs_and_errors(self):
        for adapter in self.adapters:
            result = adapter.invoke(InvocationRequest("datasci", "datasci_run_code", {"code_str": "print(sum(i*i for i in range(10000)))"}, "session", call_id="request-parity", run_id="run-parity", timeout=10))
            self.assertEqual(result.status, "ok", result.error)
            self.assertEqual(result.result, "333283335000\n")
            self.assertEqual(result.request_id, "request-parity")
            self.assertEqual(result.run_id, "run-parity")
            self.assertGreater(result.execution_elapsed_ms, 0)
            failed = adapter.invoke(InvocationRequest("datasci", "datasci_run_code", {"code_str": "print('before-error')\n1/0"}, "session", call_id="request-error", timeout=10))
            self.assertEqual(failed.status, "error")
            self.assertIn("ZeroDivisionError", failed.error)
            self.assertIn("before-error", failed.result)
            timed_out = adapter.invoke(InvocationRequest("datasci", "datasci_run_code", {"code_str": "print('before-timeout')\nwhile True: pass"}, "session", call_id="request-timeout", timeout=10))
            self.assertEqual(timed_out.status, "timeout", timed_out.error)
            self.assertIn("before-timeout", timed_out.result)
            if isinstance(adapter, MCPRuntimeAdapter):
                stopped = adapter.invoke(InvocationRequest("datasci", "datasci_run_code", {"code_str": "print('silently-reset')"}, "session", timeout=10))
                self.assertEqual(stopped.status, "error")
                self.assertIn("create a new Session", stopped.error)
        for server in self.servers:
            events = [json.loads(line) for line in server.log_path.read_text().splitlines() if line.startswith('{')]
            errors = [event for event in events if event.get("event") == "tool_execution" and event["status"] != "ok"]
            self.assertEqual({event["status"] for event in errors}, {"error", "timeout"})
            for event in errors:
                for key in ("request_id", "run_id", "timestamp_utc", "monotonic_ns", "traceback"):
                    self.assertTrue(event.get(key), key)

    def test_sixteen_faas_sessions_in_a_shared_server(self):
        adapter = self.adapters[0]
        def session(index):
            uid = f"transport-{index}"
            for code, expected in ((f"value = {index}\nopen('value', 'w').write(str(value))\nprint(value*value)", f"{index*index}\n"),
                                   ("print(value, open('value').read())", f"{index} {index}\n")):
                result = adapter.invoke(InvocationRequest("datasci", "datasci_run_code", {"code_str": code}, uid, timeout=30))
                self.assertEqual(result.status, "ok", result.error)
                self.assertEqual(result.result, expected)
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(session, range(16)))

    def test_legacy_client_return_shapes_are_preserved(self):
        # This test precedes the intentional timeout that stops the resident worker.
        for adapter in self.adapters:
            target = adapter._target("datasci")
            client = adapter._client(target) if isinstance(adapter, FunctionRuntimeAdapter) else adapter._client(target, "session")
            result = client.invoke("datasci_run_code", {"uid": "session", "code_str": "print(1)"}, request_id="legacy-id")
            self.assertEqual(result, ("legacy-id", True) if isinstance(adapter, FunctionRuntimeAdapter) else "legacy-id")


class EvidenceTests(unittest.TestCase):
    def test_mcp_iserror_and_missing_response(self):
        result = mcp_response({"result": {"isError": True, "content": [{"type": "text", "text": "bad tool"}]}}, request_id="r")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error, "bad tool")
        with self.assertRaises(ValueError):
            mcp_response(None)

    def test_jsonl_writes_are_complete_under_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"run.jsonl"
            log = EventLog(path, run_id="run", uid="uid")
            with ThreadPoolExecutor(max_workers=16) as pool:
                list(pool.map(lambda index: log.write("done", request_id=f"r-{index}"), range(128)))
            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(events), 128)
            self.assertEqual(len({event["request_id"] for event in events}), 128)
            self.assertTrue(all(event["timestamp_utc"].endswith("+00:00") for event in events))
            with self.assertRaises(FileExistsError):
                EventLog(path, run_id="another", uid="another")

class FailureEvidenceTests(unittest.TestCase):
    def test_missing_provider_metrics_remain_null_and_observed_zero_is_valid(self):
        from atsuite.analysis.aggregation import ReportAggregator
        from atsuite.analysis.model import InvocationObservation, NodeObservation, ProviderMetric, RunContext
        context = RunContext("u", "test", "test", "Bench", "trace", "faas", "config", start_time=1, end_time=2)
        node = NodeObservation(1, "tool", "function", elapsed_ms=100)
        invocation = InvocationObservation(1, "tool", "target", "runtime", "faas", "tool", "call", "u")
        report = ReportAggregator().aggregate(context, nodes={1: node}, invocations=[invocation], metrics=[], costs=[], diagnostics=[], evidence=[])
        for key in ("total_compute_time_ms", "total_tool_exec_ms", "total_price", "cold_start_total", "avg_memory_usage_mb"):
            self.assertIsNone(report.summary[key], key)
        self.assertTrue(any(item["code"] == "missing_metrics" for item in report.diagnostics))
        metric = ProviderMetric("metric", "test", "test", node_id=1, invocation_call_id="call", fields={"tool_exec_ms": 0, "duration_ms": 100, "memory_usage_mb": 0, "init_duration_ms": 0, "is_cold_start": False})
        report = ReportAggregator().aggregate(context, nodes={1: node}, invocations=[invocation], metrics=[metric], costs=[], diagnostics=[], evidence=[])
        self.assertEqual(report.summary["total_compute_time_ms"], 0)
        self.assertEqual(report.summary["avg_memory_usage_mb"], 0)

    def test_concurrent_replays_have_unique_artifacts_and_keep_timeout_diagnostics(self):
        from atsuite import invoker
        from atsuite.pipeline import resolve_benchmark
        from atsuite.runtime import InvocationResult, RuntimeCapabilities, RuntimeSession
        class Runtime:
            def capabilities(self):
                return RuntimeCapabilities("session", True, True, False)
            def connect(self, mapping):
                pass
            def open_session(self, target, uid):
                return RuntimeSession(uid, target.target_id)
            def close_session(self, session):
                pass
            def cleanup_run(self, uid):
                pass
            def invoke(self, request):
                return InvocationResult(status="timeout" if request.uid == "timeout" else "ok", result="hello",
                                        request_id=request.call_id, run_id=request.run_id, error="deadline" if request.uid == "timeout" else "")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/"Bench"
            (root/"config").mkdir(parents=True)
            (root/"nodes"/"tool").mkdir(parents=True)
            (root/"trace").mkdir()
            trace = {"nodes": [{"id": 0, "name": "start", "type": "logic", "edge_to": [{"id": 1}]}, {"id": 1, "name": "tool.run", "type": "tool_use", "edge_to": []}]}
            (root/"trace"/"trace.json").write_text(json.dumps(trace))
            config = root/"config"/"config.json"
            config.write_text(json.dumps({"trace_file": "trace/trace.json", "nodes": [{"name": "tool", "dir": "nodes/tool", "trace_names": [{"name": "tool.run", "tool": "tool"}]}], "pipeline": {"mcp_serverless": {"servers": [{"name": "server", "nodes": ["tool"], "deploy": {"cpu": 1, "memory": 1024, "disk": 512, "timeout": 30}}]}}}))
            mapping = root/"mapping.json"
            mapping.write_text(json.dumps({"targets": {"server": "http://example.test"}}))
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                with mock.patch.object(invoker, "create_runtime_adapter", side_effect=lambda *_: Runtime()), mock.patch.object(invoker, "cleaner", return_value=0):
                    def run(uid):
                        return invoker.run_trace(config, mapping, uid, provider="mcp_gateway", skip_sleep=True, skip_analyzer=True)
                    with ThreadPoolExecutor(max_workers=16) as pool:
                        results = list(pool.map(run, ["same"]*16))
                    self.assertEqual(len({item["run"]["run_id"] for item in results}), 16)
                    with self.assertRaisesRegex(RuntimeError, "deadline"):
                        run("timeout")
                logs = list(Path("results/mcp_gateway/Bench").glob("*.jsonl"))
                reports = [json.loads(path.read_text()) for path in Path("results/mcp_gateway/Bench").glob("*.result.json")]
                self.assertEqual(len(logs), 17)
                self.assertEqual(len(reports), 17)
                failed = next(item for item in reports if item["status"] == "timeout")
                self.assertIn("traceback", failed)
                events = [json.loads(line) for line in Path(failed["event_log_path"]).read_text().splitlines()]
                self.assertTrue(any(event.get("status") == "timeout" and event.get("event") == "tool_response" for event in events))
                self.assertIs(sys.stdout, sys.__stdout__)
            finally:
                os.chdir(previous)
                resolve_benchmark.cache_clear()


class CompatibilityTests(unittest.TestCase):
    def test_excel_preserves_unavailable_metrics_and_records_coverage(self):
        import pandas as pd
        from atsuite.cli.concurrency import save_unified_results
        with tempfile.TemporaryDirectory() as tmp:
            path = save_unified_results(Path(tmp), "test", [
                {"index": 1, "uid": "u1", "compute_time_ms": 0, "total_price": None},
                {"index": 2, "uid": "u2", "compute_time_ms": None, "total_price": None},
            ])
            rows = pd.read_excel(path, sheet_name="results")
            coverage = pd.read_excel(path, sheet_name="coverage").set_index("metric")
            self.assertTrue(rows["total_price"].isna().all())
            self.assertEqual(rows.loc[0, "compute_time_ms"], 0)
            self.assertTrue(pd.isna(rows.loc[1, "compute_time_ms"]))
            self.assertEqual(coverage.loc["compute_time_ms", "measured"], 1)
            self.assertEqual(coverage.loc["total_price", "measured"], 0)

    def test_notebook_read_isolated_by_uid_without_state_commit(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ATSUITE_SESSION_ROOT": tmp, "PROVIDER": ""}):
            runtime = state.StateRuntime()
            with mock.patch.object(state, "_RUNTIME", runtime):
                notebook = importlib.import_module("benchmarks.TravelPlanner.nodes.notebook.implementation")
                proxy = runtime.register_object("notebook", notebook.Notebook(), notebook.__name__, session_scoped=True)
                with mock.patch.object(notebook, "notebook", proxy):
                    notebook.notebook_write.execute_from_dict({"uid": "first", "input_data": '[{"n":1}]', "short_description": "first"})
                    notebook.notebook_write.execute_from_dict({"uid": "second", "input_data": '[{"n":2}]', "short_description": "second"})
                    with mock.patch.object(runtime, "save_after_tool", wraps=runtime.save_after_tool) as save:
                        self.assertEqual(notebook.notebook_list_all.execute_from_dict({"uid": "first"})[0]["content"], [{"n":1}])
                        self.assertEqual(notebook.notebook_list_all.execute_from_dict({"uid": "second"})[0]["content"], [{"n":2}])
                        save.assert_not_called()

    def test_mcp_rpc_errors_and_iserror_cannot_be_marked_success_by_metadata(self):
        result = mcp_response({"error": {"code": -32602, "message": "invalid params"}}, provider_request_id="provider")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.provider_request_id, "provider")
        result = mcp_response({"result": {"isError": True, "content": [], "_meta": {"atsuite": {"status": "ok"}}}})
        self.assertEqual(result.status, "error")


if __name__ == "__main__":
    unittest.main()
