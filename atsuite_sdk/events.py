"""One JSON object per event, without redirecting process-wide output streams."""

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_OUTPUT_LOCK = threading.Lock()
_PROCESS_ID = uuid.uuid4().hex
_OBSERVED_START_NS = time.time_ns()
_OBSERVED_MONOTONIC_NS = time.perf_counter_ns()


def runtime_metadata():
    """Identify an observed process interval, never infer billable instance lifetime."""
    return {
        "process_id": _PROCESS_ID,
        "pid": os.getpid(),
        "provider_environment_id": os.getenv("FC_INSTANCE_ID")
        or os.getenv("AWS_LAMBDA_LOG_STREAM_NAME"),
        "observed_start_wall_ns": _OBSERVED_START_NS,
        "observed_at_wall_ns": time.time_ns(),
        "observed_uptime_s": (time.perf_counter_ns() - _OBSERVED_MONOTONIC_NS) / 1e9,
        "scope": "application_process_not_billable_instance",
    }


def event_payload(event, **fields):
    return {
        "event": event,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "monotonic_ns": time.perf_counter_ns(),
        "runtime": runtime_metadata(),
        **fields,
    }


def emit_event(event, **fields):
    line = json.dumps(event_payload(event, **fields), ensure_ascii=False)
    with _OUTPUT_LOCK:
        print(line, flush=True)


class EventLog:
    def __init__(self, path: Path, *, run_id: str, uid: str):
        self.path, self.run_id, self.uid = path, run_id, uid
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=False)

    def write(self, event, **fields):
        line = json.dumps(
            event_payload(event, run_id=self.run_id, uid=self.uid, **fields),
            ensure_ascii=False,
        )
        with self.lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
