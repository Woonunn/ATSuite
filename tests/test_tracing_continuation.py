"""Real LangGraph checkpoints and nested graphs, without external services."""

import asyncio
import json
import operator
import pickle
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from atsuite.tracing import TraceCollector
from atsuite.tracing.langgraph import LangGraphTracingAdapter
from atsuite_sdk.workflow import Trace


class State(TypedDict):
    values: Annotated[list, operator.add]


async def run(adapter, graph, inputs, config=None):
    states = [state async for state in adapter.astream(graph, inputs, config)]
    return states[-1] if states else None


def tasks(collector, name=None):
    return [s for s in collector.spans.values() if s["kind"] == "workflow" and (name is None or s["name"] == name)]


def assert_dag(test, directory):
    path = Path(directory) / "trace.atg.json"
    Trace.from_file(path)
    nodes = json.loads(path.read_text())["nodes"]
    incoming = {n["id"]: 0 for n in nodes}
    for node in nodes:
        for edge in node["edge_to"]:
            incoming[edge["id"]] += 1
    test.assertEqual([n for n, count in incoming.items() if count == 0], [0])
    ready = [0]
    visited = set()
    while ready:
        node = ready.pop()
        visited.add(node)
        for edge in nodes[node]["edge_to"]:
            incoming[edge["id"]] -= 1
            if incoming[edge["id"]] == 0:
                ready.append(edge["id"])
    test.assertEqual(len(visited), len(nodes), "Exported containment/continuation edges must form a DAG")


def save_checkpoint(saver, path):
    # Trusted test fixture only; the adapter never loads pickled application state.
    payload = (
        {thread: dict(namespaces) for thread, namespaces in saver.storage.items()},
        dict(saver.writes),
        dict(saver.blobs),
    )
    Path(path).write_bytes(pickle.dumps(payload))


def load_checkpoint(path):
    storage, writes, blobs = pickle.loads(Path(path).read_bytes())
    saver = InMemorySaver()
    for thread, namespaces in storage.items():
        saver.storage[thread].update(namespaces)
    saver.writes.update(writes)
    saver.blobs.update(blobs)
    return saver


def static_graph(saver):
    builder = StateGraph(State)
    for name in ("before", "gate", "after"):
        builder.add_node(name, lambda s, name=name: {"values": [name]})
    builder.add_edge(START, "before")
    builder.add_edge("before", "gate")
    builder.add_edge("gate", "after")
    builder.add_edge("after", END)
    return builder.compile(checkpointer=saver, interrupt_before=["gate"], interrupt_after=["gate"])


class ContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_same_names_parallel_branches(self):
        model = FakeListChatModel(responses=["result"])

        async def work(state):
            await model.ainvoke("nested call")
            await asyncio.sleep(0.01)
            return {"values": ["work"]}

        inner = StateGraph(State)
        inner.add_node("route", lambda s: {})
        inner.add_node("work", work)
        inner.add_edge(START, "route")
        inner.add_conditional_edges("route", lambda s: Send("work", s), ["work"])
        inner.add_edge("work", END)
        inner = inner.compile(name="inner")
        child = StateGraph(State)
        child.add_node("route", lambda s: {})
        child.add_node("nested", inner)
        child.add_edge(START, "route")
        child.add_edge("route", "nested")
        child.add_edge("nested", END)
        child = child.compile(name="child")
        parent = StateGraph(State)
        parent.add_node("route", lambda s: {})
        parent.add_node("left", child)
        parent.add_node("right", child)
        parent.add_node("join", lambda s: {})
        parent.add_edge(START, "route")
        parent.add_conditional_edges(
            "route", lambda s: [Send("left", {"values": []}), Send("right", {"values": []})], ["left", "right"]
        )
        parent.add_edge(["left", "right"], "join")
        parent.add_edge("join", END)
        graph = parent.compile()
        with tempfile.TemporaryDirectory() as directory:
            collector = TraceCollector("nested", "s", Path(directory))
            result = await run(LangGraphTracingAdapter(collector), graph, {"values": []})
            self.assertEqual(result, await graph.ainvoke({"values": []}))
            self.assertEqual(collector.diagnostics, [])
            self.assertTrue(collector.snapshot()["summary"]["successful"])
            self.assertEqual(collector.snapshot()["summary"]["llm_calls"], 2)
            self.assertEqual(collector.snapshot()["summary"]["parallel_workflow_peak"], 2)
            self.assertEqual(len(tasks(collector, "route")), 5)
            for span in tasks(collector, "work"):
                parent = collector.spans[span["parent_id"]]
                self.assertEqual(parent["name"], "nested")
                for dep in span["dependencies"]:
                    self.assertEqual(
                        collector.spans[dep]["metadata"]["graph_namespace"], span["metadata"]["graph_namespace"]
                    )
            assert_dag(self, directory)

    async def test_undiscoverable_runtime_subgraph_stays_incomplete(self):
        async def wrapper(state):
            child = StateGraph(State)
            child.add_node("dynamic", lambda s: {"values": ["dynamic"]})
            child.add_edge(START, "dynamic")
            child.add_edge("dynamic", END)
            return await child.compile().ainvoke(state)

        parent = StateGraph(State)
        parent.add_node("wrapper", wrapper)
        parent.add_edge(START, "wrapper")
        parent.add_edge("wrapper", END)
        collector = TraceCollector("r", "s")
        result = await run(LangGraphTracingAdapter(collector), parent.compile(), {"values": []})
        self.assertEqual(result["values"], ["dynamic"])
        self.assertFalse(collector.snapshot()["summary"]["complete"])
        self.assertIsNone(collector.snapshot()["summary"]["rho_kb_s"])
        self.assertTrue(any("Unresolved subgraph" in d for d in collector.diagnostics))

    async def test_calls_before_and_after_wrapped_subgraph_keep_causal_order(self):
        model = FakeListChatModel(responses=["ok"])

        async def inner_call(state):
            await model.ainvoke("inside")
            return {}

        child = StateGraph(State)
        child.add_node("inner", inner_call)
        child.add_edge(START, "inner")
        child.add_edge("inner", END)
        child = child.compile(name="child").with_config(run_name="renamed")

        async def wrapper(state):
            await model.ainvoke("before")
            result = await child.ainvoke(state)
            await model.ainvoke("after")
            return result

        parent = StateGraph(State)
        parent.add_node("wrapper", wrapper)
        parent.add_edge(START, "wrapper")
        parent.add_edge("wrapper", END)
        with tempfile.TemporaryDirectory() as directory:
            collector = TraceCollector("r", "s", Path(directory))
            await run(LangGraphTracingAdapter(collector), parent.compile(), {"values": []})
            llms = [s for s in collector.spans.values() if s["kind"] == "llm"]
            inner = tasks(collector, "inner")[0]
            self.assertEqual(collector.diagnostics, [])
            self.assertIn(llms[0]["id"], inner["dependencies"])
            self.assertIn(inner["id"], llms[-1]["dependencies"])
            self.assertEqual(collector.snapshot()["summary"]["llm_calls"], 3)
            assert_dag(self, directory)

    async def test_parent_command_leaves_subgraph(self):
        child = StateGraph(State)
        child.add_node("jump", lambda s: Command(graph=Command.PARENT, goto="last", update={"values": ["child"]}))
        child.add_edge(START, "jump")
        child.add_edge("jump", END)
        parent = StateGraph(State)
        parent.add_node("sub", child.compile())
        parent.add_node("last", lambda s: {"values": ["parent"]})
        parent.add_edge(START, "sub")
        parent.add_edge("last", END)
        collector = TraceCollector("r", "s")
        result = await run(LangGraphTracingAdapter(collector), parent.compile(), {"values": []})
        self.assertEqual(result["values"], ["child", "parent"])
        self.assertEqual(collector.diagnostics, [])
        self.assertEqual(tasks(collector, "last")[0]["dependencies"], [tasks(collector, "sub")[0]["id"]])
        self.assertTrue(all(s["status"] == "ok" for s in collector.spans.values()))

    async def test_interrupt_resume_retains_actual_reexecuted_calls(self):
        model = FakeListChatModel(responses=["ok"])

        async def gate(state):
            await model.ainvoke("reexecuted before interrupt")
            return {"values": [interrupt("approval")]}

        builder = StateGraph(State)
        builder.add_node("before", lambda s: {"values": ["before"]})
        builder.add_node("gate", gate)
        builder.add_node("after", lambda s: {"values": ["after"]})
        builder.add_edge(START, "before")
        builder.add_edge("before", "gate")
        builder.add_edge("gate", "after")
        builder.add_edge("after", END)
        graph = builder.compile(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "interrupt"}}
        with tempfile.TemporaryDirectory() as directory:
            collector = TraceCollector("r", "s", Path(directory))
            adapter = LangGraphTracingAdapter(collector)
            paused = await run(adapter, graph, {"values": []}, config)
            self.assertIn("__interrupt__", paused)
            self.assertEqual(tasks(collector, "gate")[0]["status"], "interrupted")
            self.assertFalse(collector.snapshot()["summary"]["complete"])
            self.assertFalse(collector.snapshot()["summary"]["successful"])
            self.assertIsNone(collector.snapshot()["summary"]["rho_kb_s"])
            result = await run(adapter, graph, Command(resume="approved"), config)
            self.assertEqual(result["values"], ["before", "approved", "after"])
            summary = collector.snapshot()["summary"]
            self.assertTrue(summary["complete"])
            self.assertTrue(summary["successful"])
            self.assertEqual(summary["execution_segments"], 2)
            self.assertEqual(summary["llm_calls"], 2)
            self.assertEqual(len(tasks(collector, "before")), 1)
            attempts = tasks(collector, "gate")
            self.assertEqual(attempts[1]["metadata"]["retry_of"], attempts[0]["id"])
            self.assertIn(attempts[0]["id"], attempts[1]["dependencies"])
            self.assertEqual(tasks(collector, "after")[0]["dependencies"], [attempts[1]["id"]])
            self.assertEqual(collector.diagnostics, [])
            assert_dag(self, directory)

    async def test_static_breakpoints_and_saved_collector(self):
        saver = InMemorySaver()
        graph = static_graph(saver)
        config = {"configurable": {"thread_id": "static"}}
        with tempfile.TemporaryDirectory() as directory:
            collector = TraceCollector("r", "s", Path(directory))
            await run(LangGraphTracingAdapter(collector), graph, {"values": []}, config)
            self.assertFalse(collector.snapshot()["summary"]["complete"])
            checkpoint_path = Path(directory) / "checkpoint.fixture"
            save_checkpoint(saver, checkpoint_path)
            script = """
import asyncio, sys
from pathlib import Path
from atsuite.tracing import TraceCollector
from atsuite.tracing.langgraph import LangGraphTracingAdapter
from tests.test_tracing_continuation import load_checkpoint, save_checkpoint, static_graph, run
root = Path(sys.argv[1]); saver = load_checkpoint(root / 'checkpoint.fixture')
collector = TraceCollector.load(root)
asyncio.run(run(LangGraphTracingAdapter(collector), static_graph(saver), None,
                {'configurable': {'thread_id': 'static'}}))
save_checkpoint(saver, root / 'checkpoint.fixture')
"""
            completed = await asyncio.to_thread(
                subprocess.run, [sys.executable, "-c", script, directory], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            collector = TraceCollector.load(Path(directory))
            graph = static_graph(load_checkpoint(checkpoint_path))
            self.assertFalse(collector.snapshot()["summary"]["complete"])
            collector = TraceCollector.load(Path(directory))
            result = await run(LangGraphTracingAdapter(collector), graph, None, config)
            self.assertEqual(result["values"], ["before", "gate", "after"])
            self.assertEqual(len(tasks(collector)), 3)
            self.assertEqual(collector.snapshot()["summary"]["execution_segments"], 3)
            self.assertTrue(collector.snapshot()["summary"]["complete"])
            self.assertEqual(collector.diagnostics, [])
            assert_dag(self, directory)

    async def test_failed_parallel_branch_reuses_completed_pending_write(self):
        calls = Counter()
        model = FakeListChatModel(responses=["ok"])

        async def worker(state):
            label = state["values"][0]
            calls[label] += 1
            await model.ainvoke(label)
            if label == "bad" and calls[label] == 1:
                await asyncio.sleep(0.01)
                raise ValueError("temporary failure")
            return {"values": [label]}

        graph = StateGraph(State)
        graph.add_node("dispatch", lambda s: {})
        graph.add_node("worker", worker)
        graph.add_node("join", lambda s: {})
        graph.add_edge(START, "dispatch")
        graph.add_conditional_edges(
            "dispatch", lambda s: [Send("worker", {"values": [label]}) for label in ("good", "bad")], ["worker"]
        )
        graph.add_edge("worker", "join")
        graph.add_edge("join", END)
        graph = graph.compile(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "failure"}}
        with tempfile.TemporaryDirectory() as directory:
            collector = TraceCollector("r", "s", Path(directory))
            with self.assertRaisesRegex(ValueError, "temporary failure"):
                await run(LangGraphTracingAdapter(collector), graph, {"values": []}, config)
            collector = TraceCollector.load(Path(directory))
            result = await run(LangGraphTracingAdapter(collector), graph, None, config)
            self.assertEqual(sorted(result["values"]), ["bad", "good"])
            self.assertEqual(calls, {"good": 1, "bad": 2})
            self.assertEqual(collector.snapshot()["summary"]["llm_calls"], 3)
            self.assertTrue(collector.snapshot()["summary"]["successful"])
            self.assertTrue(collector.snapshot()["summary"]["complete"])
            successful = [s["id"] for s in tasks(collector, "worker") if s["status"] == "ok"]
            self.assertEqual(set(tasks(collector, "join")[0]["dependencies"]), set(successful))
            self.assertEqual(collector.diagnostics, [])
            assert_dag(self, directory)

    async def test_nested_interrupt_saved_resume_links_old_children(self):
        calls = Counter()

        def before(state):
            calls["before"] += 1
            return {"values": ["before"]}

        def gate(state):
            calls["gate"] += 1
            return {"values": [interrupt("nested approval")]}

        child = StateGraph(State)
        child.add_node("before", before)
        child.add_node("gate", gate)
        child.add_edge(START, "before")
        child.add_edge("before", "gate")
        child.add_edge("gate", END)
        parent = StateGraph(State)
        parent.add_node("sub", child.compile())
        parent.add_node("after", lambda s: {"values": ["after"]})
        parent.add_edge(START, "sub")
        parent.add_edge("sub", "after")
        parent.add_edge("after", END)
        graph = parent.compile(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "nested"}}
        with tempfile.TemporaryDirectory() as directory:
            collector = TraceCollector("r", "s", Path(directory))
            await run(LangGraphTracingAdapter(collector), graph, {"values": []}, config)
            self.assertEqual(len(collector.harness_state["langgraph"]["checkpoints"]), 2)
            collector = TraceCollector.load(Path(directory))
            result = await run(LangGraphTracingAdapter(collector), graph, Command(resume="approved"), config)
            self.assertEqual(result["values"], ["before", "approved", "after"])
            self.assertEqual(calls, {"before": 1, "gate": 2})
            self.assertEqual(collector.diagnostics, [])
            self.assertTrue(collector.snapshot()["summary"]["complete"])
            gates = tasks(collector, "gate")
            self.assertNotEqual(gates[0]["parent_id"], gates[1]["parent_id"])
            self.assertIn(tasks(collector, "before")[0]["id"], gates[1]["dependencies"])
            assert_dag(self, directory)

    async def test_wrong_checkpoint_is_rejected_and_missing_history_stays_unknown(self):
        graph = StateGraph(State)
        graph.add_node("gate", lambda s: {"values": [interrupt("approval")]})
        graph.add_edge(START, "gate")
        graph.add_edge("gate", END)
        graph = graph.compile(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "correct"}}
        collector = TraceCollector("r", "s")
        adapter = LangGraphTracingAdapter(collector)
        await run(adapter, graph, {"values": []}, config)
        before = collector.snapshot()
        with self.assertRaisesRegex(ValueError, "checkpoint/thread"):
            await run(adapter, graph, Command(resume="no"), {"configurable": {"thread_id": "wrong"}})
        self.assertEqual(before, collector.snapshot())
        partial = TraceCollector("partial", "s")
        await run(LangGraphTracingAdapter(partial), graph, Command(resume="approved"), config)
        self.assertFalse(partial.snapshot()["summary"]["complete"])
        self.assertIsNone(partial.snapshot()["summary"]["rho_kb_s"])
        with self.assertRaisesRegex(ValueError, "checkpoint/thread"):
            await run(adapter, graph, None, config)

    async def test_multiple_interrupts_keep_attempts_and_do_not_fabricate_checkpoint_bytes(self):
        model = FakeListChatModel(responses=["ok"])

        async def gate(state):
            await model.ainvoke("attempt")
            first = interrupt("approval")
            second = interrupt("approval")
            return {"values": [first, second]}

        graph = StateGraph(State)
        graph.add_node("gate", gate)
        graph.add_edge(START, "gate")
        graph.add_edge("gate", END)
        graph = graph.compile(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "multiple"}}
        collector = TraceCollector("r", "s")
        adapter = LangGraphTracingAdapter(collector)
        await run(adapter, graph, {"values": []}, config)
        first_binding = json.loads(json.dumps(collector.harness_state))
        await run(adapter, graph, Command(resume="first"), config)
        self.assertFalse(collector.snapshot()["summary"]["complete"])
        self.assertNotEqual(first_binding, collector.harness_state)
        result = await run(adapter, graph, Command(resume="second"), config)
        self.assertEqual(result["values"], ["first", "second"])
        self.assertEqual(collector.snapshot()["summary"]["llm_calls"], 3)
        self.assertTrue(collector.snapshot()["summary"]["complete"])
        self.assertFalse(collector.snapshot()["summary"]["state_measurements_complete"])
        self.assertIsNone(collector.snapshot()["summary"]["state_transfer_bytes"])
        self.assertIsNone(collector.snapshot()["summary"]["rho_kb_s"])
        self.assertEqual(collector.diagnostics, [])

    async def test_stale_trace_cannot_reuse_a_checkpoint_with_new_resume_writes(self):
        def gate(state):
            first = interrupt("same question")
            second = interrupt("same question")
            return {"values": [first, second]}

        graph = StateGraph(State)
        graph.add_node("gate", gate)
        graph.add_edge(START, "gate")
        graph.add_edge("gate", END)
        graph = graph.compile(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "stale"}}
        collector = TraceCollector("r", "s")
        adapter = LangGraphTracingAdapter(collector)
        await run(adapter, graph, {"values": []}, config)
        checkpoint_ids = collector.harness_state["langgraph"]["checkpoints"]
        await graph.ainvoke(Command(resume="untraced"), config)
        snapshot = await graph.aget_state(config)
        self.assertEqual(checkpoint_ids[""], snapshot.config["configurable"]["checkpoint_id"])
        with self.assertRaisesRegex(ValueError, "checkpoint/thread"):
            await run(adapter, graph, Command(resume="second"), config)

    async def test_checkpoint_observation_failure_preserves_the_business_error(self):
        class Saver(InMemorySaver):
            broken = False

            async def aget_tuple(self, config):
                if self.broken:
                    raise OSError("checkpoint observation failed")
                return await super().aget_tuple(config)

        saver = Saver()

        def fail(state):
            saver.broken = True
            raise ValueError("actual business error")

        graph = StateGraph(State)
        graph.add_node("fail", fail)
        graph.add_edge(START, "fail")
        graph.add_edge("fail", END)
        graph = graph.compile(checkpointer=saver)
        with tempfile.TemporaryDirectory() as directory:
            collector = TraceCollector("r", "s", Path(directory))
            with self.assertRaisesRegex(ValueError, "actual business error"):
                await run(
                    LangGraphTracingAdapter(collector), graph, {"values": []}, {"configurable": {"thread_id": "error"}}
                )
            self.assertIn("Checkpoint observation failed", collector.diagnostics[0])
            self.assertFalse(collector.snapshot()["summary"]["complete"])
            self.assertTrue((Path(directory) / "trace.json").exists())

    async def test_unfinished_segment_marker_keeps_lost_history_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trace_dir = root / "live"
            abandoned_dir = root / "abandoned"
            abandoned_dir.mkdir()

            async def node(state):
                payload = json.loads((trace_dir / "trace.json").read_text())
                self.assertTrue(payload["harness_state"]["in_progress"])
                self.assertFalse(payload["summary"]["complete"])
                (abandoned_dir / "trace.json").write_text(json.dumps(payload))
                return {"values": ["done"]}

            graph = StateGraph(State)
            graph.add_node("node", node)
            graph.add_edge(START, "node")
            graph.add_edge("node", END)
            collector = TraceCollector("r", "s", trace_dir)
            await run(LangGraphTracingAdapter(collector), graph.compile(), {"values": []})
            self.assertTrue(collector.snapshot()["summary"]["complete"])
            self.assertFalse(collector.harness_state["in_progress"])
            abandoned = TraceCollector.load(abandoned_dir)
            self.assertFalse(abandoned.snapshot()["summary"]["complete"])
            self.assertTrue(any("history may be incomplete" in d for d in abandoned.diagnostics))

    async def test_concurrent_checkpoint_threads_stay_separate(self):
        graph = StateGraph(State)
        graph.add_node("gate", lambda s: {"values": [interrupt("approval")]})
        graph.add_edge(START, "gate")
        graph.add_edge("gate", END)
        graph = graph.compile(checkpointer=InMemorySaver())

        async def request(index):
            collector = TraceCollector(str(index), str(index))
            adapter = LangGraphTracingAdapter(collector)
            config = {"configurable": {"thread_id": str(index)}}
            await run(adapter, graph, {"values": []}, config)
            result = await run(adapter, graph, Command(resume=index), config)
            self.assertEqual(result["values"], [index])
            self.assertEqual(collector.diagnostics, [])
            return collector

        collectors = await asyncio.gather(*(request(i) for i in range(3)))
        ids = [set(c.spans) for c in collectors]
        self.assertEqual(len(set.union(*ids)), sum(map(len, ids)))


if __name__ == "__main__":
    unittest.main()
