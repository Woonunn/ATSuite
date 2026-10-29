"""Execute pinned LangGraph graphs; no network or model account required."""

import asyncio
import json
import operator
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, TypedDict
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send

from atsuite.tracing import TraceCollector
from atsuite.tracing.export import export_trace
from atsuite.tracing.langgraph import LangGraphTracingAdapter
from atsuite_sdk.workflow import Trace


class State(TypedDict):
    values: Annotated[list, operator.add]
    count: int


class TracingTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, graph, initial=None, directory=None, request="r"):
        collector = TraceCollector(request, request, directory)
        adapter = LangGraphTracingAdapter(collector)
        states = [s async for s in adapter.astream(graph, initial or {"values": [], "count": 0})]
        return collector, states[-1]

    async def test_dynamic_send_loop_native_calls_and_dag(self):
        model = FakeListChatModel(responses=["yes"])

        @tool
        async def echo(value: str) -> str:
            """Return the value."""
            await asyncio.sleep(0)
            return value

        async def plan(state):
            await model.ainvoke("choose")
            return {"count": state["count"] + 1}

        async def work(state):
            return {"values": [await echo.ainvoke(str(state["count"]))]}

        def route(state):
            return [Send("work", state), Send("work", state)]

        graph = StateGraph(State)
        graph.add_node("plan", plan)
        graph.add_node("work", work)
        graph.add_node("merge", lambda s: {})
        graph.add_edge(START, "plan")
        graph.add_conditional_edges("plan", route, ["work"])
        graph.add_edge("work", "merge")
        graph.add_conditional_edges("merge", lambda s: "plan" if s["count"] < 2 else END)
        graph = graph.compile()
        with tempfile.TemporaryDirectory() as directory:
            collector, result = await self.collect(graph, directory=Path(directory))
            self.assertEqual(result, await graph.ainvoke({"values": [], "count": 0}))
            trace = collector.snapshot()
            self.assertEqual(trace["diagnostics"], [])
            self.assertEqual(trace["summary"]["llm_calls"], 2)
            self.assertEqual(trace["summary"]["tool_calls"], 4)
            self.assertIsNone(trace["summary"]["rho_kb_s"])
            tasks = [s for s in trace["spans"] if s["kind"] == "workflow"]
            self.assertEqual([s["name"] for s in tasks], ["plan", "work", "work", "merge"] * 2)
            self.assertEqual(tasks[1]["dependencies"], [tasks[0]["id"]])
            self.assertEqual(tasks[2]["dependencies"], [tasks[0]["id"]])
            self.assertEqual(set(tasks[3]["dependencies"]), {tasks[1]["id"], tasks[2]["id"]})
            self.assertEqual(tasks[4]["dependencies"], [tasks[3]["id"]])
            self.assertFalse((Path(directory) / "trace.atg.json").exists())
            loaded = Trace.from_file(export_trace(Path(directory), "atg"))
            self.assertEqual(sum(n.node_type == "llm" for n in loaded.nodes), 2)
            # Every exported node is reachable, and a topological traversal consumes all nodes.
            nodes = json.loads((Path(directory) / "trace.atg.json").read_text())["nodes"]
            incoming = {n["id"]: 0 for n in nodes}
            for n in nodes:
                for e in n["edge_to"]:
                    incoming[e["id"]] += 1
            ready = [n for n, count in incoming.items() if count == 0]
            visited = []
            while ready:
                n = ready.pop()
                visited.append(n)
                for e in nodes[n]["edge_to"]:
                    incoming[e["id"]] -= 1
                    if incoming[e["id"]] == 0:
                        ready.append(e["id"])
            self.assertEqual(len(visited), len(nodes))
            self.assertEqual(
                [n for n in incoming if not any(e["id"] == n for row in nodes for e in row["edge_to"])],
                [0],
            )

    async def test_conditional_path_map_and_skewed_join(self):
        graph = StateGraph(State)
        for name in ("choose", "short", "long", "later", "join", "never"):
            graph.add_node(name, lambda s: {})
        graph.add_edge(START, "choose")
        graph.add_conditional_edges(
            "choose",
            lambda s: ["left", "right"],
            {"left": "short", "right": "long", "unused": "never"},
        )
        graph.add_edge("long", "later")
        graph.add_edge(["short", "later"], "join")
        graph.add_edge("join", END)
        collector, _ = await self.collect(graph.compile())
        self.assertEqual(collector.diagnostics, [])
        tasks = {s["name"]: s for s in collector.spans.values() if s["kind"] == "workflow"}
        self.assertNotIn("never", tasks)
        self.assertEqual(
            set(tasks["join"]["dependencies"]),
            {tasks["short"]["id"], tasks["later"]["id"]},
        )
        self.assertEqual(tasks["later"]["dependencies"], [tasks["long"]["id"]])

    async def test_datasci_bridge_retries_and_runtime_correlation(self):
        import httpx
        from openai import RateLimitError

        from atsuite.runtime import RuntimeSession
        from atsuite_sdk.response import ToolResponse
        from benchmarks.DataSciBench.langgraph_agent.model_client import DeepSeekClient
        from benchmarks.DataSciBench.langgraph_agent.tool_client import ToolClient
        from benchmarks.DataSciBench.langgraph_agent.trace_recorder import TraceRecorder

        error = RateLimitError(
            "retry", response=httpx.Response(429, request=httpx.Request("POST", "https://example.org")), body={}
        )
        raw = {"id": "response", "model": "test", "usage": {"prompt_tokens": 1, "completion_tokens": 2}}
        response = SimpleNamespace(
            model_dump=lambda **kw: raw,
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="{}"))],
            model="test",
            id="response",
        )
        api = MagicMock()
        api.api_key = "fixture"
        api.chat.completions.create = AsyncMock(side_effect=[error, response])
        runtime = MagicMock()
        runtime.targets = {"t": object()}
        runtime.open_session.return_value = RuntimeSession(uid="u", target_id="t")
        runtime.invoke.return_value = ToolResponse(
            result={"status": "ok"},
            request_id="runtime-request",
            provider_metadata={
                "state_sync": {"load_bytes": 11, "save_bytes": 7, "branch_read_bytes": 3, "branch_write_bytes": 5}
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            collector = TraceCollector("r", "s")
            adapter = LangGraphTracingAdapter(collector)
            recorder = TraceRecorder(Path(directory) / "capture", "r", "s", adapter)
            with (
                patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fixture"}),
                patch("benchmarks.DataSciBench.langgraph_agent.model_client.AsyncOpenAI", return_value=api),
                patch(
                    "benchmarks.DataSciBench.langgraph_agent.tool_client.create_runtime_adapter", return_value=runtime
                ),
            ):
                model = DeepSeekClient(
                    recorder, {"seed": 0, "temperature": 0, "top_p": 1, "max_tokens": 100, "retries": 1}
                )
                tools = ToolClient(
                    {"provider": "local", "family": "faas", "target_id": "t", "endpoint": "fixture"}, recorder
                )

                async def node(state):
                    with patch(
                        "benchmarks.DataSciBench.langgraph_agent.model_client.asyncio.sleep", new_callable=AsyncMock
                    ):
                        await model.decide("PLAN", 0, [])
                    await tools.call("execute", {}, "key")
                    return {}

                graph = StateGraph(State)
                graph.add_node("calls", node)
                graph.add_edge(START, "calls")
                graph.add_edge("calls", END)
                async for _ in adapter.astream(graph.compile(), {"count": 0, "values": []}):
                    pass
                await tools.aclose()
            trace = collector.snapshot()
            summary = trace["summary"]
            self.assertTrue(summary["complete"])
            self.assertEqual(summary["llm_calls"], 1)
            self.assertEqual(summary["llm_attempts"], 2)
            self.assertEqual(summary["tool_calls"], 1)
            self.assertEqual(summary["state_transfer_bytes"], 26)
            llm = next(s for s in trace["spans"] if s["kind"] == "llm")
            call = next(s for s in trace["spans"] if s["kind"] == "tool")
            self.assertEqual(llm["metadata"]["attempt_errors"][0]["status_code"], 429)
            self.assertEqual(call["dependencies"], [llm["id"]])
            self.assertEqual(call["metadata"]["runtime_uid"], tools.uid)
            self.assertEqual(call["metadata"]["call_id"], runtime.invoke.call_args.args[0].call_id)

    async def test_send_sources_follow_numeric_task_paths(self):
        graph = StateGraph(State)
        graph.add_node("root", lambda s: {})
        graph.add_node("source", lambda s: Command(goto=Send("leaf", s)), destinations=["leaf"])
        graph.add_node("leaf", lambda s: {"values": [s["count"]]})
        graph.add_edge(START, "root")
        graph.add_conditional_edges(
            "root",
            lambda s: [Send("source", {"count": i, "values": []}) for i in range(12)],
            ["source"],
        )
        graph.add_edge("leaf", END)
        collector, result = await self.collect(graph.compile())
        self.assertEqual(sorted(result["values"]), list(range(12)))
        self.assertEqual(collector.diagnostics, [])
        for span in collector.spans.values():
            if span["name"] == "leaf":
                parent = collector.spans[span["dependencies"][0]]
                self.assertEqual(parent["input"]["count"], span["input"]["count"])

    async def test_same_graph_concurrent_sessions_and_error(self):
        async def node(state):
            await asyncio.sleep(0)
            if state["count"] < 0:
                raise ValueError("actual failure")
            return {"values": [state["count"]]}

        graph = StateGraph(State)
        graph.add_node("node", node)
        graph.add_edge(START, "node")
        graph.add_edge("node", END)
        graph = graph.compile()
        outputs = await asyncio.gather(
            *(self.collect(graph, {"values": [], "count": i}, request=str(i)) for i in range(4))
        )
        ids = [set(c.spans) for c, _ in outputs]
        self.assertEqual(len(set.union(*ids)), sum(map(len, ids)))
        for i, (collector, result) in enumerate(outputs):
            self.assertEqual(result["values"], [i])
            self.assertEqual(collector.request_id, str(i))
        with tempfile.TemporaryDirectory() as directory:
            collector = TraceCollector("failed", "session", Path(directory))
            with self.assertRaisesRegex(ValueError, "actual failure"):
                async for _ in LangGraphTracingAdapter(collector).astream(graph, {"values": [], "count": -1}):
                    pass
            self.assertTrue(any(s["status"] == "error" for s in collector.spans.values()))
            self.assertTrue((Path(directory) / "trace.json.gz").is_file())

    def test_rho_requires_logical_measurements_and_counts_repeated_io(self):
        collector = TraceCollector("r", "s")
        collector.start("session", "session", "graph")
        collector.end("session")
        collector.start("llm", "llm", "model")
        collector.end("llm")
        collector.spans["llm"]["duration_s"] = 2
        for index in range(2):
            rid = str(index)
            collector.start(rid, "tool", "tool")
            collector.end(rid)
            collector.observe_runtime(
                rid,
                {
                    "provider_metadata": {
                        "state_sync": {
                            "load_bytes": 2000,
                            "save_bytes": 1000,
                            "branch_read_bytes": 500,
                            "branch_write_bytes": 500,
                        }
                    }
                },
            )
        self.assertIsNone(collector.snapshot()["summary"]["rho_kb_s"])
        for index in range(2):
            collector.observe_state_accesses(str(index), [
                {"object_id": "state", "operation": "read", "bytes": 2500},
                {"object_id": "state", "operation": "write", "bytes": 1500},
            ])
        self.assertEqual(collector.snapshot()["summary"]["rho_kb_s"], 4)
        collector.start("unknown", "tool", "old runtime")
        collector.end("unknown")
        summary = collector.snapshot()["summary"]
        self.assertIsNone(summary["rho_kb_s"])
        self.assertIsNone(summary["state_transfer_bytes"])
        self.assertEqual(summary["observed_state_transfer_bytes"], 8000)


class RuntimeByteTests(unittest.TestCase):
    def test_snapshot_actual_bytes_repeated_reads_and_mcp_zero(self):
        from atsuite_sdk.state import StateRuntime

        with tempfile.TemporaryDirectory() as directory:
            for family in ("function", "mcp"):
                with patch.dict(
                    os.environ,
                    {
                        "ATSUITE_RUNTIME": family,
                        "PROVIDER": "",
                        "ATSUITE_SESSION_ROOT": directory,
                    },
                ):
                    runtime = StateRuntime()
                obj = runtime.register_object(
                    "value",
                    SimpleNamespace(value="数据"),
                    __name__,
                    session_scoped=True,
                )
                with runtime.execution(__name__, "uid", "rw"):
                    obj.value = "实际内容"
                metrics = runtime.get_sync_metrics()
                if family == "mcp":
                    self.assertEqual(metrics["load_bytes"], 0)
                    self.assertEqual(metrics["save_bytes"], 0)
                else:
                    size = (Path(directory) / ".snapshots" / runtime._snapshot_key("uid")).stat().st_size
                    self.assertEqual(metrics["save_bytes"], size)
                    runtime.reset_sync_metrics()
                    runtime._get_snapshot("uid")
                    runtime._get_snapshot("uid")
                    self.assertEqual(runtime.get_sync_metrics()["load_bytes"], size * 2)

                    def read(_):
                        with runtime.execution(__name__, "uid", "r"):
                            pass
                        return runtime.get_sync_metrics()

                    with ThreadPoolExecutor(max_workers=2) as pool:
                        for measured in pool.map(read, range(4)):
                            self.assertEqual(measured["load_bytes"], size)
                            self.assertEqual(measured["save_bytes"], 0)

    def test_failed_transfer_is_unknown_and_cannot_be_replaced_by_zero(self):
        from atsuite_sdk.state import StateRuntime

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"ATSUITE_RUNTIME": "function", "PROVIDER": "", "ATSUITE_SESSION_ROOT": directory}),
        ):
            runtime = StateRuntime()
            runtime.reset_sync_metrics()
            runtime._storage = MagicMock()
            runtime._storage.read.side_effect = OSError("partial read")
            with self.assertRaises(OSError):
                runtime._get_snapshot("u")
            self.assertIsNone(runtime.get_sync_metrics()["load_bytes"])
            runtime.record_transfer_bytes("load_bytes", 12)
            self.assertIsNone(runtime.get_sync_metrics()["load_bytes"])
            runtime._storage.put.side_effect = OSError("partial write")
            with self.assertRaises(OSError):
                runtime._set_snapshot("u", {"version": 1})
            self.assertIsNone(runtime.get_sync_metrics()["save_bytes"])

    def test_branch_bytes_use_actual_bundle_size(self):
        from atsuite_sdk.state import StateRuntime
        from benchmarks.DataSciBench.nodes.AgentTools import implementation

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "ATSUITE_RUNTIME": "mcp",
                    "PROVIDER": "",
                    "ATSUITE_SESSION_ROOT": directory,
                },
            ),
        ):
            runtime = StateRuntime()
            runtime.reset_sync_metrics()
            source = Path(directory) / "source"
            source.write_bytes(b"actual branch payload")
            with patch.object(implementation, "get_state_runtime", return_value=runtime):
                implementation.store("bundle", source=source)
                for i in range(2):
                    implementation.store("bundle", destination=Path(directory) / str(i))
            metrics = runtime.get_sync_metrics()
            self.assertEqual(metrics["branch_write_bytes"], source.stat().st_size)
            self.assertEqual(metrics["branch_read_bytes"], source.stat().st_size * 2)


if __name__ == "__main__":
    unittest.main()
