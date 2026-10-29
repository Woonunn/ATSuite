"""Storage changes preserve trace/replay data, derived graphs and recoverable logs."""

import gzip
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from atsuite.runtime import RuntimeSession
from atsuite.tracing import TraceCollector
from atsuite.tracing.export import export_trace
from atsuite.tracing.storage import compress_log, read_text, write_json
from atsuite_sdk.response import ToolResponse
from atsuite_sdk.workflow import Trace
from benchmarks.DataSciBench.langgraph_agent.tool_client import ToolClient
from benchmarks.DataSciBench.langgraph_agent.trace_recorder import TraceRecorder
from benchmarks.DataSciBench.langgraph_agent.trace_replayer import ReplayModel


class TraceStorageTests(unittest.TestCase):
    def collector(self, directory):
        collector = TraceCollector("request", "session", directory)
        collector.start("root", "session", "graph")
        collector.start("node", "workflow", "analysis", parent_id="root")
        collector.start("call", "tool", "execute", parent_id="node", inputs={"code": "数据" * 5000})
        collector.end("call", {"status": "ok", "unknown": None})
        collector.end("node", {"result": 1})
        collector.end("root", {"status": "complete"})
        collector.harness_state = {"thread_id": "request", "pending": ["resume"]}
        return collector

    def test_canonical_gzip_roundtrip_and_explicit_exports(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            collector = self.collector(directory)
            expected = collector.snapshot()
            collector.save()
            self.assertEqual({p.name for p in directory.iterdir()}, {"trace.json.gz"})
            self.assertEqual(json.loads(read_text(directory / "trace.json")), expected)
            self.assertEqual(TraceCollector.load(directory).snapshot(), expected)
            self.assertLess((directory / "trace.json.gz").stat().st_size, len(json.dumps(expected).encode()))
            spans = export_trace(directory, "spans")
            self.assertEqual([json.loads(line) for line in spans.read_text().splitlines()], expected["spans"])
            self.assertFalse((directory / "trace.atg.json").exists())
            atg = Trace.from_file(export_trace(directory, "atg"))
            self.assertEqual(sum(n.node_type == "tool_use" for n in atg.nodes), 1)

    def test_historical_plain_trace_remains_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            expected = self.collector(directory).snapshot()
            (directory / "trace.json").write_text(json.dumps(expected), encoding="utf-8")
            self.assertEqual(TraceCollector.load(directory).snapshot(), expected)
            self.assertTrue(export_trace(directory, "atg").is_file())
            write_json(directory / "trace.json", {**expected, "schema_version": 2})
            with self.assertRaisesRegex(ValueError, "Unsupported trace schema"):
                TraceCollector.load(directory)
            with self.assertRaisesRegex(ValueError, "Unsupported trace schema"):
                export_trace(directory, "spans")

    def test_failed_atomic_save_preserves_the_last_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            collector = self.collector(directory)
            collector.save()
            previous = (directory / "trace.json.gz").read_bytes()
            collector.harness_state["extra"] = "new data"
            with patch("atsuite.tracing.storage.Path.replace", side_effect=OSError("storage failed")):
                with self.assertRaisesRegex(OSError, "storage failed"):
                    collector.save()
            self.assertEqual((directory / "trace.json.gz").read_bytes(), previous)
            self.assertFalse((directory / "trace.json.gz.tmp").exists())
            with self.assertRaises(ValueError):
                write_json(directory / "trace.json", {"invalid": float("nan")})
            self.assertEqual((directory / "trace.json.gz").read_bytes(), previous)

    def test_failed_log_compression_preserves_live_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            original = b'{"seq":0}\n'
            path.write_bytes(original)
            with patch("atsuite.tracing.storage.gzip.GzipFile", side_effect=OSError("compression failed")):
                with self.assertRaisesRegex(OSError, "compression failed"):
                    compress_log(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(path.with_name("events.jsonl.gz").exists())
            self.assertFalse(path.with_name("events.jsonl.gz.tmp").exists())
            compress_log(path)
            self.assertFalse(path.exists())
            self.assertEqual(gzip.decompress(path.with_name("events.jsonl.gz").read_bytes()), original)

    def test_corrupted_gzip_does_not_silently_load_a_stale_plain_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trace.json"
            path.write_text('{"stale":true}')
            path.with_name("trace.json.gz").write_bytes(b"invalid gzip")
            with self.assertRaises(gzip.BadGzipFile):
                read_text(path)


class CompressedReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_finished_capture_replays_from_three_gzip_files(self):
        from experiments.exp1.runner.result_collector import read_events

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "capture"
            collector = TraceCollector("r", "s", directory)
            tracing = SimpleNamespace(collector=collector, record_event=lambda event: None)
            recorder = TraceRecorder(directory, "r", "s", tracing)
            llm = recorder.emit(
                "llm", phase="PLAN", iteration=0, duration_s=0.25, request={"messages": []}, decision={"steps": []}
            )
            recorder.emit("tool", key="call", tool_name="execute", args={"code": "pass"}, result={"status": "ok"})
            before = (directory / "events.jsonl").read_bytes()
            self.assertEqual(len(read_events(directory / "events.jsonl")), 2)
            recorder.finish({"status": "complete"})
            self.assertEqual(
                {p.name for p in directory.iterdir()}, {"trace.json.gz", "events.jsonl.gz", "result.json.gz"}
            )
            self.assertEqual(gzip.decompress((directory / "events.jsonl.gz").read_bytes()), before)
            self.assertEqual(read_events(directory / "events.jsonl"), recorder.events)
            replay_recorder = TraceRecorder(root / "replay", "r2", "s2")
            model = ReplayModel(directory, replay_recorder)
            with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
                self.assertEqual(await model.decide("PLAN", 0, []), llm["decision"])
                sleep.assert_awaited_once_with(0.25)
            model.assert_consumed()
            runtime = MagicMock()
            runtime.targets = {"t": object()}
            runtime.open_session.return_value = RuntimeSession(uid="u", target_id="t")
            runtime.invoke.return_value = ToolResponse(result={"status": "ok"})
            with patch(
                "benchmarks.DataSciBench.langgraph_agent.tool_client.create_runtime_adapter", return_value=runtime
            ):
                tools = ToolClient(
                    {"provider": "local", "family": "faas", "target_id": "t", "endpoint": "fixture"},
                    replay_recorder,
                    directory,
                )
                self.assertEqual(await tools.call("execute", {"code": "pass"}, "call"), {"status": "ok"})
                tools.assert_consumed()
                await tools.aclose()
            replay_recorder.finish({"status": "complete"})
            self.assertEqual(read_events(root / "missing.jsonl"), [])
