"""Local test fixtures: one MCP server process represents exactly one Session."""
import importlib
import os
import runpy
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
MODULE = "benchmarks.DataSciBench.nodes.DataSci.implementation"


class LocalToolEnvironment:
    def __init__(self, root, family, uid):
        self.root, self.family, self.uid = Path(root), family, uid
        self.root.mkdir(parents=True, exist_ok=True)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.log_path = self.root / "server.jsonl"
        self.log = self.log_path.open("w")
        env = dict(os.environ, PROVIDER="", ATSUITE_RUNTIME=family,
                   ATSUITE_SESSION_ROOT=str(self.root / "state"), ATSUITE_NODE_MODULE=MODULE,
                   ATSUITE_MCP_PORT=str(port), FC_SERVER_PORT=str(port), ATSUITE_MCP_STATELESS="true",
                   ATSUITE_DATASCI_TIMEOUT="2", PYTHONPATH=str(ROOT), NO_COLOR="1")
        command = ([sys.executable, "-m", "atsuite_sdk.function"] if family == "function"
                   else [sys.executable, str(Path(__file__).resolve())])
        self.process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        self.url = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 60
            while True:
                if self.process.poll() is not None:
                    raise RuntimeError(self.log_path.read_text())
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.1):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Local Tool environment startup timed out")
                    time.sleep(.02)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.log.close()


def serve_mcp():
    importlib.import_module(MODULE)
    from atsuite_sdk.abstract import registry
    from atsuite_sdk.state import register_state_object
    resident = register_state_object("probe", SimpleNamespace(value=0, token=object(), items=[], guard=threading.Lock(), active=0))
    readers = threading.Barrier(16)

    def info():
        return {"value": resident.value, "identity": id(resident.token), "pid": os.getpid(), "items": list(resident.items), "cwd": os.getcwd()}

    @registry.tool(access="r", domain="probe")
    def memory_info() -> dict:
        return info()

    @registry.tool(access="rw", domain="probe")
    def memory_bump() -> dict:
        with resident.guard:
            assert resident.active == 0, "write overlapped a reader"
        resident.value += 1
        resident.items.append(resident.value)
        return info()

    @registry.tool(access="r", domain="probe")
    def memory_parallel_read() -> dict:
        with resident.guard:
            resident.active += 1
        try:
            readers.wait(timeout=20)
            return info()
        finally:
            with resident.guard:
                resident.active -= 1

    runpy.run_module("atsuite_sdk.mcp_server", run_name="__main__")


if __name__ == "__main__":
    serve_mcp()
