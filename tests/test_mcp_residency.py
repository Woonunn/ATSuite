"""Separate same-process lock tests from local, process-isolated MCP environments."""
import json
import os
import random
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from atsuite.runtime import InvocationRequest, MCPRuntimeAdapter
from atsuite_sdk.abstract import UnifiedAgentTool
from atsuite_sdk import state
from phase0_environment import LocalToolEnvironment


class MCPSameProcessAccessTests(unittest.TestCase):
    """Unit simulations of ONE Session; these do not prove platform isolation."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"ATSUITE_RUNTIME": "mcp", "ATSUITE_SESSION_ROOT": self.tmp.name, "PROVIDER": "aws_agentcore"})
        self.env.start()
        self.addCleanup(self.env.stop)
        with mock.patch("atsuite_sdk.storage.create_storage") as storage:
            self.runtime = state.StateRuntime()
        storage.assert_not_called()
        self.patch = mock.patch.object(state, "_RUNTIME", self.runtime)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.obj = self.runtime.register_object("counter", SimpleNamespace(value=0, items=[], _identity=object()), __name__, session_scoped=True)
        self.assertNotIsInstance(self.obj, state.SessionObject)

    def test_continuous_live_memory_without_snapshot_copy_reset_commit_or_flock(self):
        identity = self.obj._identity
        def append():
            self.obj.value += 1
            self.obj.items.append(self.obj.value)
            self.obj.dynamic = identity
            with self.runtime.session_open("continuity", "a") as output:
                output.write(str(self.obj.value))
            return self.obj.value
        tool = UnifiedAgentTool(append, access="rw")
        with mock.patch.object(self.runtime, "load_for_tool", side_effect=AssertionError("MCP restore")), \
             mock.patch.object(self.runtime, "save_after_tool", side_effect=AssertionError("MCP commit")), \
             mock.patch.object(self.runtime, "_apply_object_state", side_effect=AssertionError("MCP reset")), \
             mock.patch.object(self.runtime, "_get_snapshot", side_effect=AssertionError("MCP snapshot")), \
             mock.patch("atsuite_sdk.state.copy.deepcopy", side_effect=AssertionError("MCP copy")), \
             mock.patch("atsuite_sdk.state.fcntl.flock", side_effect=AssertionError("MCP file lock")):
            self.assertEqual([tool.execute_from_dict({"uid": "one"}) for _ in range(3)], [1, 2, 3])
            reader = UnifiedAgentTool(lambda: self.obj.value, access="r")
            self.assertEqual(reader.execute_from_dict({"uid": "one"}), 3)
        self.assertIs(self.obj._identity, identity)
        self.assertIs(self.obj.dynamic, identity)
        self.assertEqual(self.obj.items, [1, 2, 3])
        self.assertEqual(self.runtime.session_path("continuity", "one").read_text(), "123")
        self.assertFalse(list(Path(self.tmp.name).rglob(".snapshots")))
        self.assertFalse(list(Path(self.tmp.name).rglob(".locks")))

    def test_parallel_readers_exclude_writers_but_not_stateless_calls(self):
        for access in ("w", "rw"):
            entered, release, wrote = threading.Barrier(17), threading.Event(), threading.Event()
            def read():
                entered.wait(timeout=10)
                self.assertTrue(release.wait(timeout=10))
                return self.obj.value
            def write():
                wrote.set()
                self.obj.value += 1
                return self.obj.value
            reader = UnifiedAgentTool(read, access="r", domain="same")
            writer = UnifiedAgentTool(write, access=access, domain="same")
            stateless = UnifiedAgentTool(lambda: "free", access="stateless", domain="same")
            before = self.obj.value
            with ThreadPoolExecutor(max_workers=18) as pool:
                readers = [pool.submit(reader.execute_from_dict, {"uid": "one"}) for _ in range(16)]
                entered.wait(timeout=10)
                future = pool.submit(writer.execute_from_dict, {"uid": "one"})
                self.assertEqual(pool.submit(stateless.execute_from_dict, {"uid": "one"}).result(timeout=2), "free")
                self.assertFalse(wrote.is_set())
                release.set()
                self.assertEqual([item.result(timeout=10) for item in readers], [before]*16)
                self.assertEqual(future.result(timeout=10), before+1)

    def test_nonconflicting_domains_can_execute_together(self):
        gate = threading.Barrier(2)
        def write():
            gate.wait(timeout=5)
            return "ok"
        tools = [UnifiedAgentTool(write, access="rw", domain=domain) for domain in ("a", "b")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(tool.execute_from_dict, {"uid": "one"}) for tool in tools]
            self.assertEqual([item.result(timeout=10) for item in results], ["ok", "ok"])

    def test_explicit_bootstrap_once_does_not_enable_per_call_restoration(self):
        def increment():
            self.obj.value += 1
            return self.obj.value
        tool = UnifiedAgentTool(increment, access="rw")
        snapshot = {"version": 1, "vars": {__name__: {"counter": {"value": 5, "items": []}}}, "files": {}}
        self.assertEqual(tool.execute_from_dict({"uid": "one", "__atsuite_state_snapshot": snapshot}), 6)
        self.assertEqual(tool.execute_from_dict({"uid": "one"}), 7)
        with self.assertRaisesRegex(ValueError, "reseed"):
            tool.execute_from_dict({"uid": "one", "__atsuite_state_snapshot": snapshot})
        self.assertEqual(self.obj.value, 7)

    def test_misrouted_uid_is_rejected_instead_of_simulating_shared_process_sessions(self):
        tool = UnifiedAgentTool(lambda: self.obj.value, access="r")
        tool.execute_from_dict({"uid": "one"})
        with self.assertRaisesRegex(ValueError, "another Session"):
            tool.execute_from_dict({"uid": "two"})


class MCPSeparateEnvironmentTests(unittest.TestCase):
    """Actual local OS-process environments; no claim of cloud/provider validation."""
    def test_sixteen_processes_preserve_live_python_files_and_rpc_access_rules(self):
        servers, adapters = [], []
        with tempfile.TemporaryDirectory() as tmp:
            try:
                with ThreadPoolExecutor(max_workers=16) as pool:
                    servers = list(pool.map(lambda index: LocalToolEnvironment(Path(tmp)/str(index), "mcp", f"session-{index}"), range(16)))
                for server in servers:
                    adapter = MCPRuntimeAdapter("test")
                    adapter.connect({"targets": {"datasci": server.url}})
                    adapters.append(adapter)
                def call(index, tool, args=None):
                    result = adapters[index].invoke(InvocationRequest("datasci", tool, args or {}, f"session-{index}", timeout=60))
                    self.assertEqual(result.status, "ok", result.error)
                    return result.result
                def session(index):
                    first = call(index, "datasci_run_code", {"code_str":
                        "import os, json, random\n"
                        "class Marker: pass\n"
                        "marker = Marker()\n"
                        "def squared(x): return x*x\n"
                        f"values = [{index}, {index+1}, {index+2}]\n"
                        f"rng = random.Random({index})\nfirst_random = rng.random()\n"
                        f"handle = open('input.txt', 'w+')\nhandle.write(str({index}))\nhandle.flush()\nhandle.seek(0)\n"
                        "print(json.dumps({'pid':os.getpid(), 'identity':id(marker), 'cwd':os.getcwd(), 'sum':sum(values)}))"})
                    second = call(index, "datasci_run_code", {"code_str":
                        "print(json.dumps({'pid':os.getpid(), 'identity':id(marker), 'cwd':os.getcwd(), 'file':handle.read(), 'square':squared(sum(values)), 'random':rng.random()}))"})
                    a, b = json.loads(first), json.loads(second)
                    self.assertEqual((a["pid"], a["identity"], a["cwd"]), (b["pid"], b["identity"], b["cwd"]))
                    self.assertEqual(b["file"], str(index))
                    self.assertEqual(b["square"], (3*index+3)**2)
                    rng = random.Random(index)
                    rng.random()
                    self.assertEqual(b["random"], rng.random())
                    wrong = adapters[index].invoke(InvocationRequest("datasci", "memory_info", {}, "another-session", timeout=30))
                    self.assertEqual(wrong.status, "error")
                    return {"uid": f"session-{index}", "server_pid": servers[index].process.pid, "worker_pid": a["pid"], "namespace_identity": a["identity"]}
                with ThreadPoolExecutor(max_workers=16) as pool:
                    sessions = list(pool.map(session, range(16)))
                self.assertEqual(len({row["server_pid"] for row in sessions}), 16)
                self.assertEqual(len({row["worker_pid"] for row in sessions}), 16)
                initial = call(0, "memory_info")
                self.assertEqual(call(0, "datasci_run_code", {"code_str": "os.write(1, b'native-stdout\\n'); os.write(2, b'native-stderr\\n')"}), "native-stdout\nnative-stderr\n")
                child_cwd = call(0, "datasci_run_code", {"code_str": "os.mkdir('child-cwd'); os.chdir('child-cwd'); print(os.getcwd())"}).strip()
                self.assertEqual(call(0, "datasci_run_code", {"code_str": "print(os.getcwd())"}).strip(), child_cwd)
                self.assertEqual(call(0, "memory_info")["cwd"], initial["cwd"])

                with ThreadPoolExecutor(max_workers=16) as pool:
                    increments = list(pool.map(lambda _: call(0, "memory_bump"), range(16)))
                self.assertEqual(sorted(row["value"] for row in increments), list(range(1, 17)))
                with ThreadPoolExecutor(max_workers=16) as pool:
                    reads = list(pool.map(lambda _: call(0, "memory_parallel_read"), range(16)))
                self.assertTrue(all(row["value"] == 16 and row["identity"] == initial["identity"] for row in reads))
                # An ordinary Python error keeps the live namespace; no hidden reload.
                failed = adapters[0].invoke(InvocationRequest("datasci", "datasci_run_code", {"code_str": "1/0"}, "session-0", timeout=30))
                self.assertEqual(failed.status, "error")
                self.assertEqual(call(0, "datasci_run_code", {"code_str": "print(squared(7), id(marker))"}), f"49 {sessions[0]['namespace_identity']}\n")
                for server in servers:
                    self.assertFalse(list(server.root.rglob(".snapshots")))
                    self.assertFalse(list(server.root.rglob(".locks")))
                self._evidence = {"model": "local_process_per_session", "cloud_validation": False, "sessions": sessions,
                                  "same_session_parallel_readers": 16, "same_session_serialized_writes": 16}
            finally:
                for adapter in adapters:
                    for client in adapter._clients.values():
                        client.close()
                for server in servers:
                    server.close()
                artifact = os.getenv("ATSUITE_TEST_ARTIFACT_DIR")
                if artifact:
                    target = Path(artifact)/"mcp-environments"
                    shutil.copytree(tmp, target, dirs_exist_ok=True)
                    (target/"isolation.json").write_text(json.dumps(getattr(self, "_evidence", {"status": "failed"}), indent=2)+"\n")


class FaaSMultipleInstanceTests(unittest.TestCase):
    def test_separate_instance_roots_serialize_writes_through_conditional_store(self):
        # Local object-store model, not a provider or billing test.
        class Store:
            def __init__(self, root):
                self.root = root
            def read(self, key):
                path = self.root/key
                return path.read_bytes() if path.exists() else b""
            def put(self, key, data):
                path = self.root/key
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(f".{threading.get_ident()}.tmp")
                temporary.write_bytes(data)
                temporary.replace(path)
            def try_create(self, key, data):
                path = self.root/key
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with path.open("xb") as output:
                        output.write(data)
                    return True
                except FileExistsError:
                    return False
            def deleteobj(self, key):
                (self.root/key).unlink()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instances = []
            for index in range(2):
                with mock.patch.dict(os.environ, {"ATSUITE_RUNTIME": "function", "ATSUITE_SESSION_ROOT": str(root/str(index)), "PROVIDER": ""}):
                    runtime = state.StateRuntime()
                runtime._storage = Store(root/"remote")
                obj = runtime.register_object("counter", SimpleNamespace(value=0), __name__, session_scoped=True)
                instances.append((runtime, obj))
            def increment(index):
                runtime, obj = instances[index % 2]
                with runtime.execution(__name__, "shared", "rw"):
                    obj.value += 1
                    return obj.value
            with ThreadPoolExecutor(max_workers=16) as pool:
                values = list(pool.map(increment, range(16)))
            self.assertEqual(sorted(values), list(range(1, 17)))
            for runtime, obj in instances:
                with runtime.execution(__name__, "shared", "r"):
                    self.assertEqual(obj.value, 16)


if __name__ == "__main__":
    unittest.main()
