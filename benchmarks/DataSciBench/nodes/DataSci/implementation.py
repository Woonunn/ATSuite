import importlib
import atexit
import select
import threading
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

from atsuite_sdk.abstract import registry
from atsuite_sdk.state import get_state_runtime, register_state_object

_NODE_DIR = Path(__file__).resolve().parent
_SESSION_PARENT_SEED_FILES = (
    ("bootstrap/data_pums_2000.csv", "data_pums_2000.csv"),
    ("bootstrap/adult.data", "adult.data"),
    ("bootstrap/adult.test", "adult.test"),
)
_STATE_TYPE_KEY = "__atsuite_state_type__"
_DATAFRAME_STATE = "dataframe"
_SERIES_STATE = "series"


class DataSciWorkspace:
    def __init__(self) -> None:
        self.variables: dict[str, Any] = {}
        self.imported_modules: dict[str, str] = {}
        self._worker = None

    def namespace(self) -> dict[str, Any]:
        ns: dict[str, Any] = {"__builtins__": __builtins__}
        for alias, module_name in self.imported_modules.items():
            ns[alias] = importlib.import_module(module_name)
        for name, value in self.variables.items():
            ns[name] = _from_state_value(value)
        return ns

    def sync_from_namespace(self, ns: dict[str, Any]) -> None:
        saved_variables: dict[str, Any] = {}
        imported_modules: dict[str, str] = {}

        for name, value in ns.items():
            if name.startswith("__"):
                continue
            if isinstance(value, ModuleType):
                imported_modules[name] = value.__name__
                continue
            if callable(value):
                continue

            try:
                converted = _to_state_value(value)
            except TypeError:
                continue
            else:
                saved_variables[name] = converted

        self.variables = saved_variables
        self.imported_modules = imported_modules

    def reset(self) -> None:
        if self._worker is not None:
            self._worker.close()
            self._worker = None
        self.variables = {}
        self.imported_modules = {}

    def names(self) -> list[str]:
        return sorted(set(self.imported_modules.keys()) | set(self.variables.keys()))


datasci_workspace = register_state_object("datasci_workspace", DataSciWorkspace(), session_scoped=True)


def _to_state_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    dataframe_state = _maybe_dataframe_state(value)
    if dataframe_state is not None:
        return dataframe_state
    series_state = _maybe_series_state(value)
    if series_state is not None:
        return series_state
    if hasattr(value, "tolist"):
        try:
            return _to_state_value(value.tolist())
        except Exception:
            raise TypeError("unsupported state value") from None
    if hasattr(value, "item"):
        try:
            return _to_state_value(value.item())
        except Exception:
            raise TypeError("unsupported state value") from None
    if isinstance(value, dict):
        converted_dict: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("unsupported state value")
            converted_item = _to_state_value(item)
            converted_dict[key] = converted_item
        return converted_dict
    if isinstance(value, (list, tuple)):
        converted_list = []
        for item in value:
            converted_list.append(_to_state_value(item))
        return converted_list
    raise TypeError("unsupported state value")


def _maybe_dataframe_state(value: Any) -> Optional[dict[str, Any]]:
    if not _is_dataframe_like(value):
        return None
    payload = value.to_dict(orient="split")
    if not isinstance(payload, dict):
        raise TypeError("unsupported dataframe state")
    return {
        _STATE_TYPE_KEY: _DATAFRAME_STATE,
        "value": {
            "index": _to_state_value(payload.get("index", [])),
            "columns": _to_state_value(payload.get("columns", [])),
            "data": _to_state_value(payload.get("data", [])),
        },
    }


def _maybe_series_state(value: Any) -> Optional[dict[str, Any]]:
    if not _is_series_like(value):
        return None
    return {
        _STATE_TYPE_KEY: _SERIES_STATE,
        "value": {
            "data": _to_state_value(value.tolist()),
            "name": _to_state_value(getattr(value, "name", None)),
            "index": _to_state_value(list(getattr(value, "index", []))),
        },
    }


def _from_state_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_from_state_value(item) for item in value]
    if not isinstance(value, dict):
        return value

    state_type = value.get(_STATE_TYPE_KEY)
    if state_type == _DATAFRAME_STATE:
        pandas = importlib.import_module("pandas")
        payload = value["value"]
        return pandas.DataFrame(
            _from_state_value(payload["data"]),
            columns=_from_state_value(payload["columns"]),
            index=_from_state_value(payload["index"]),
        )
    if state_type == _SERIES_STATE:
        pandas = importlib.import_module("pandas")
        payload = value["value"]
        return pandas.Series(
            _from_state_value(payload["data"]),
            name=_from_state_value(payload["name"]),
            index=_from_state_value(payload["index"]),
        )

    return {key: _from_state_value(item) for key, item in value.items()}


def _is_dataframe_like(value: Any) -> bool:
    try:
        pandas = importlib.import_module("pandas")
    except Exception:
        pandas = None

    if pandas is not None:
        dataframe_type = getattr(pandas, "DataFrame", None)
        if dataframe_type is not None and isinstance(value, dataframe_type):
            return True

    return value.__class__.__name__ == "DataFrame" and hasattr(value, "to_dict")


def _is_series_like(value: Any) -> bool:
    try:
        pandas = importlib.import_module("pandas")
    except Exception:
        pandas = None

    if pandas is not None:
        series_type = getattr(pandas, "Series", None)
        if series_type is not None and isinstance(value, series_type):
            return True

    return value.__class__.__name__ == "Series" and hasattr(value, "tolist")


def _ensure_workspace(uid: Optional[str]) -> Path:
    workspace = get_state_runtime().session_dir(uid)
    workspace.mkdir(parents=True, exist_ok=True)
    for src_rel, dst_name in _SESSION_PARENT_SEED_FILES:
        src = _NODE_DIR / src_rel
        # Legacy prompts use ../adult.data; the parent belongs to this Session.
        for dst in (workspace / dst_name, workspace.parent / dst_name):
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
    return workspace


class DataSciExecutionError(RuntimeError):
    def __init__(self, message, output=""):
        super().__init__(message)
        self.result = output


class DataSciTimeoutError(TimeoutError):
    def __init__(self, message, output):
        super().__init__(message)
        self.result = output


class _ResidentPython:
    """One persistent Python namespace per MCP execution environment/Session."""
    def __init__(self, workspace, env):
        self.lock = threading.Lock()
        self.workspace = workspace
        self.seeded = False
        self.output_path = workspace.parent / "python-output.txt"
        self.error_path = workspace.parent / "python-worker-errors.txt"
        read_fd, write_fd = os.pipe()
        try:
            with self.error_path.open("a") as errors:
                self.process = subprocess.Popen(
                    [sys.executable, "-u", str(_NODE_DIR / "worker.py"), "--resident", str(write_fd)],
                    cwd=workspace, env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=errors, text=True, pass_fds=(write_fd,), start_new_session=True,
                )
        except BaseException:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
        self.response = os.fdopen(read_fd, "r", encoding="utf-8")
        atexit.register(self.close)

    def close(self):
        with self.lock:
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.process.wait()
            self.process.stdin.close()
            self.response.close()

    def execute(self, code):
        with self.lock:
            if self.process.poll() is not None:
                raise DataSciExecutionError("MCP Python worker stopped; create a new Session (resident state is unavailable)")
            payload = {"code": code, "output_path": str(self.output_path)}
            if not self.seeded:
                payload.update(variables=datasci_workspace.variables, imported_modules=datasci_workspace.imported_modules)
            try:
                self.process.stdin.write(json.dumps(payload) + "\n")
                self.process.stdin.flush()
                self.seeded = True
                ready, _, _ = select.select([self.response], [], [], float(os.getenv("ATSUITE_DATASCI_TIMEOUT", "240")))
                if not ready:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait()
                    output = self.output_path.read_text(errors="replace") if self.output_path.exists() else ""
                    raise DataSciTimeoutError("DataSci Python execution timed out; resident worker stopped; create a new Session", output)
                line = self.response.readline()
                if not line:
                    raise DataSciExecutionError("MCP Python worker exited without a response; create a new Session\n" + self.error_path.read_text())
                result = json.loads(line)
            except TimeoutError:
                raise
            except (OSError, ValueError) as exc:
                raise DataSciExecutionError(f"MCP Python worker RPC failed: {exc}") from exc
            if result["status"] != "ok":
                raise DataSciExecutionError(result["error"], result["output"])
            return result["output"]


def run_code(code_str: str, uid: Optional[str] = None) -> str:
    workspace = _ensure_workspace(uid)
    env = dict(os.environ, PROVIDER="", ATSUITE_SESSION_ROOT=str(workspace.parent / "worker-state"))
    env["PYTHONPATH"] = os.pathsep.join([str(Path(__import__("atsuite_sdk").__file__).resolve().parent.parent), env.get("PYTHONPATH", "")])
    if get_state_runtime().runtime == "mcp":
        if datasci_workspace._worker is None:
            datasci_workspace._worker = _ResidentPython(workspace, env)
        return datasci_workspace._worker.execute(code_str)
    input_path, result_path = workspace.parent / "input.json", workspace.parent / "result.json"
    input_path.write_text(json.dumps({
        "code": code_str, "variables": datasci_workspace.variables,
        "imported_modules": datasci_workspace.imported_modules,
    }), encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-u", str(_NODE_DIR / "worker.py"), str(input_path), str(result_path)],
        cwd=workspace, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=float(os.getenv("ATSUITE_DATASCI_TIMEOUT", "240")))
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        raise DataSciTimeoutError(f"DataSci Python execution timed out\n{stdout}{stderr}", stdout + stderr) from None
    if process.returncode != 0:
        raise DataSciExecutionError(f"DataSci Python failed ({process.returncode})\n{stdout}{stderr}", stdout + stderr)
    state = json.loads(result_path.read_text(encoding="utf-8"))
    datasci_workspace.variables = state["variables"]
    datasci_workspace.imported_modules = state["imported_modules"]
    return stdout + stderr


@registry.tool(stateful=True)
def datasci_run_code(code_str: str, uid: Optional[str] = None) -> str:
    return run_code(code_str, uid)


__all__ = [
    "datasci_run_code",
    "datasci_workspace",
]
