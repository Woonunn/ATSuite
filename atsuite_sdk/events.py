"""One JSON object per event, without redirecting process-wide output streams."""
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_OUTPUT_LOCK = threading.Lock()


def event_payload(event, **fields):
    return {
        "event": event,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "monotonic_ns": time.perf_counter_ns(),
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
        line = json.dumps(event_payload(event, run_id=self.run_id, uid=self.uid, **fields), ensure_ascii=False)
        with self.lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
