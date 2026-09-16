from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_STATES = {"CREATED", "VALIDATED", "PLANNED", "APPROVED", "BUILDING", "DEPLOYING", "EXPLORING", "CONFIRMING", "ANALYZING", "COMPLETED", "FAILED", "CANCEL_REQUESTED", "CANCELLED", "CLEANUP_PENDING"}


class JobStore:
    """SQLite-backed experiment state store for local CLI and Web control plane."""
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.execute("CREATE TABLE IF NOT EXISTS experiments (id TEXT PRIMARY KEY, state TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS events (experiment_id TEXT NOT NULL, seq INTEGER NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(experiment_id, seq))")
        self.connection.execute("CREATE TABLE IF NOT EXISTS idempotency (scope TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL, resource_id TEXT NOT NULL, PRIMARY KEY(scope, key))")
        self.connection.execute("CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, tenant TEXT NOT NULL, project TEXT NOT NULL, relative_path TEXT NOT NULL, media_type TEXT NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS benchmarks (id TEXT PRIMARY KEY, tenant TEXT NOT NULL, project TEXT NOT NULL, payload TEXT NOT NULL)")
        self.connection.commit()

    def create(self, experiment_id: str, payload: dict[str, Any], state: str = "CREATED") -> dict[str, Any]:
        if state not in _STATES:
            raise ValueError(f"invalid state: {state}")
        record = {"id": experiment_id, "state": state, **payload}
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.connection.execute("INSERT INTO experiments VALUES (?, ?, ?, ?)", (experiment_id, state, json.dumps(record, sort_keys=True), now))
            self.connection.commit()
        return record

    def get(self, experiment_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT payload FROM experiments WHERE id = ?", (experiment_id,)).fetchone()
        if row is None:
            raise KeyError(experiment_id)
        return json.loads(row[0])

    def transition(self, experiment_id: str, state: str, **updates: Any) -> dict[str, Any]:
        if state not in _STATES:
            raise ValueError(f"invalid state: {state}")
        with self._lock:
            record = self.get(experiment_id)
            from atsuite.recommender.search import validate_transition
            validate_transition(record["state"], state)
            record.update(updates, state=state)
            now = datetime.now(timezone.utc).isoformat()
            self.connection.execute("UPDATE experiments SET state=?, payload=?, updated_at=? WHERE id=?", (state, json.dumps(record, sort_keys=True), now, experiment_id))
            self.connection.commit()
            self.append_event(experiment_id, {"state": state, **updates})
            return record

    def append_event(self, experiment_id: str, payload: dict[str, Any]) -> int:
        with self._lock:
            row = self.connection.execute("SELECT COALESCE(MAX(seq), 0) FROM events WHERE experiment_id=?", (experiment_id,)).fetchone()
            seq = int(row[0]) + 1
            self.connection.execute("INSERT INTO events VALUES (?, ?, ?, ?)", (experiment_id, seq, str(payload.get("event", "progress")), json.dumps(payload, sort_keys=True)))
            self.connection.commit()
            return seq

    def events(self, experiment_id: str, after: int = 0) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT seq, event, payload FROM events WHERE experiment_id=? AND seq>? ORDER BY seq", (experiment_id, after)).fetchall()
        return [{"id": seq, "event": event, **json.loads(payload)} for seq, event, payload in rows]

    def claim_idempotency(self, scope: str, key: str, request_hash: str, resource_id: str) -> str:
        with self._lock:
            row = self.connection.execute("SELECT request_hash, resource_id FROM idempotency WHERE scope=? AND key=?", (scope, key)).fetchone()
            if row:
                if row[0] != request_hash:
                    raise ValueError("idempotency key was already used with a different request")
                return str(row[1])
            self.connection.execute("INSERT INTO idempotency VALUES (?, ?, ?, ?)", (scope, key, request_hash, resource_id))
            self.connection.commit()
            return resource_id

    def register_artifact(self, artifact_id: str, tenant: str, project: str, relative_path: str, media_type: str = "application/json") -> None:
        with self._lock:
            self.connection.execute("INSERT OR REPLACE INTO artifacts VALUES (?, ?, ?, ?, ?)", (artifact_id, tenant, project, relative_path, media_type))
            self.connection.commit()

    def artifact(self, artifact_id: str, tenant: str, project: str) -> dict[str, str]:
        row = self.connection.execute("SELECT relative_path, media_type FROM artifacts WHERE id=? AND tenant=? AND project=?", (artifact_id, tenant, project)).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return {"relative_path": str(row[0]), "media_type": str(row[1])}

    def register_benchmark(self, benchmark_id: str, tenant: str, project: str, payload: dict[str, Any]) -> dict[str, Any]:
        record = {"id": benchmark_id, "tenant": tenant, "project": project, **payload}
        with self._lock:
            self.connection.execute("INSERT INTO benchmarks VALUES (?, ?, ?, ?)", (benchmark_id, tenant, project, json.dumps(record, sort_keys=True)))
            self.connection.commit()
        return record

    def benchmark(self, benchmark_id: str, tenant: str, project: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT payload FROM benchmarks WHERE id=? AND tenant=? AND project=?", (benchmark_id, tenant, project)).fetchone()
        if row is None:
            raise KeyError(benchmark_id)
        return json.loads(row[0])

    def close(self):
        self.connection.close()
