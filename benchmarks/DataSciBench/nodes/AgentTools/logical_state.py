"""Logical Tool-boundary snapshots; independent of residency and wire encoding."""

import json
import time
from collections.abc import Callable

CONTRACT = "datasci-tool-snapshot-v1"


def json_size(value: object) -> int:
    """Use deterministic UTF-8 JSON for metadata, excluding transport envelopes."""
    return len(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    )


class StateObservation:
    """Collect logical accesses without making failed measurements look stateless."""

    def __init__(self) -> None:
        self.accesses = []
        self.complete = True
        self.errors = []
        self.wall_s = 0.0
        self.cpu_s = 0.0

    def record(
        self, object_id: str, operation: str, measure: Callable[[], int]
    ) -> None:
        """Measure one access; serialization errors invalidate coverage, not execution."""
        wall, cpu = time.perf_counter(), time.process_time()
        try:
            size = measure()
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ValueError("Invalid object size")
            self.accesses.append(
                {"object_id": object_id, "operation": operation, "bytes": size}
            )
        except Exception as exc:  # noqa: BLE001 - user serialization hooks may raise any exception.
            self.complete = False
            self.errors.append(f"{object_id}: {type(exc).__name__}")
        finally:
            self.wall_s += time.perf_counter() - wall
            self.cpu_s += time.process_time() - cpu

    def include(self, result: dict) -> None:
        """Combine worker and host observations exactly once."""
        observed = result.get("logical_state", {})
        if observed.get("contract") != CONTRACT:
            self.complete = False
            self.errors.append("Worker logical state observation unavailable")
            return
        self.accesses.extend(observed["accesses"])
        self.complete = self.complete and observed["complete"]
        self.errors.extend(observed["errors"])
        self.wall_s += observed["observation_s"]
        self.cpu_s += observed["observation_cpu_s"]

    def attach(self, result: dict) -> dict:
        """Attach explicit completeness and measurement overhead to a Tool result."""
        result["logical_state"] = {
            "contract": CONTRACT,
            "accesses": self.accesses,
            "complete": self.complete,
            "errors": self.errors,
            "observation_s": self.wall_s,
            "observation_cpu_s": self.cpu_s,
        }
        return result
