from __future__ import annotations

import json
import sqlite3
import threading
import time
import secrets
import contextlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_STATES = {"CREATED", "VALIDATED", "PLANNED", "APPROVED", "PREFLIGHT", "BUILDING", "DEPLOYING", "EXPLORING", "CONFIRMING", "ANALYZING", "COMPLETED", "FAILED", "CANCEL_REQUESTED", "CANCELLED", "CLEANUP_PENDING"}


class JobStore:
    """SQLite-backed experiment state store for local CLI and Web control plane."""
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.execute("CREATE TABLE IF NOT EXISTS experiments (id TEXT PRIMARY KEY, state TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS events (experiment_id TEXT NOT NULL, seq INTEGER NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(experiment_id, seq))")
        self.connection.execute("CREATE TABLE IF NOT EXISTS idempotency (scope TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL, resource_id TEXT NOT NULL, PRIMARY KEY(scope, key))")
        self.connection.execute("CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, tenant TEXT NOT NULL, project TEXT NOT NULL, relative_path TEXT NOT NULL, media_type TEXT NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS benchmarks (id TEXT PRIMARY KEY, tenant TEXT NOT NULL, project TEXT NOT NULL, payload TEXT NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS leases (experiment_id TEXT PRIMARY KEY, owner TEXT, token TEXT, expires REAL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
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
        with self._lock:
            row = self.connection.execute("SELECT payload FROM experiments WHERE id = ?", (experiment_id,)).fetchone()
            if row is None:
                raise KeyError(experiment_id)
            return json.loads(row[0])

    def transition(self, experiment_id: str, state: str, **updates: Any) -> dict[str, Any]:
        if state not in _STATES:
            raise ValueError(f"invalid state: {state}")
        token = updates.pop("lease_token", None)
        with self._transaction():
            self._check_lease(experiment_id, token)
            record = self.get(experiment_id)
            from atsuite.recommender.search import validate_transition
            validate_transition(record["state"], state)
            record.update(updates, state=state)
            now = datetime.now(timezone.utc).isoformat()
            self.connection.execute("UPDATE experiments SET state=?, payload=?, updated_at=? WHERE id=?", (state, json.dumps(record, sort_keys=True), now, experiment_id))
            self._append_event(experiment_id, {"state": state, **updates})
            return record

    def append_event(self, experiment_id: str, payload: dict[str, Any]) -> int:
        with self._transaction():
            return self._append_event(experiment_id, payload)

    def _append_event(self, experiment_id, payload):
            row = self.connection.execute("SELECT COALESCE(MAX(seq), 0) FROM events WHERE experiment_id=?", (experiment_id,)).fetchone()
            seq = int(row[0]) + 1
            self.connection.execute("INSERT INTO events VALUES (?, ?, ?, ?)", (experiment_id, seq, str(payload.get("event", "progress")), json.dumps(payload, sort_keys=True)))
            return seq

    @contextlib.contextmanager
    def _transaction(self):
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def _check_lease(self, experiment_id, token):
        if token is None:
            return  # Control-plane cancellation is independent of the worker lease.
        row = self.connection.execute("SELECT token,expires FROM leases WHERE experiment_id=?", (experiment_id,)).fetchone()
        if row is None or row[0] != token or row[1] <= time.time():
            raise RuntimeError("worker lease expired or fenced")

    def claim(self, experiment_id, owner, ttl=120, *, for_cleanup=False):
        with self._transaction():
            record = self.get(experiment_id)
            if not for_cleanup and record['state'] in {'COMPLETED','CANCELLED','FAILED','PLANNED','CREATED'}:
                return None
            row = self.connection.execute("SELECT expires FROM leases WHERE experiment_id=?", (experiment_id,)).fetchone()
            if row and row[0] > time.time():
                return None
            token = secrets.token_hex(24)
            self.connection.execute("INSERT OR REPLACE INTO leases VALUES (?,?,?,?)", (experiment_id,owner,token,time.time()+ttl))
            return token

    def renew(self, experiment_id, token, ttl=120):
        with self._transaction():
            self._check_lease(experiment_id, token)
            self.connection.execute("UPDATE leases SET expires=? WHERE experiment_id=? AND token=?", (time.time()+ttl,experiment_id,token))

    def release(self, experiment_id, token):
        with self._transaction():
            self.connection.execute("DELETE FROM leases WHERE experiment_id=? AND token=?", (experiment_id,token))

    def checkpoint(self, experiment_id, stage, updates, *, lease_token=None, status=None):
        with self._transaction():
            self._check_lease(experiment_id,lease_token)
            record=self.get(experiment_id)
            record.update(updates)
            if status:
                from atsuite.recommender.identity import digest
                stages=record.setdefault('stage_records',{})
                entry=stages.setdefault(stage,{})
                entry.update(status=status,updated_at=datetime.now(timezone.utc).isoformat())
                if status=='running':
                    entry.update(attempt=entry.get('attempt',0)+1,started_at=entry['updated_at'],
                        input_digest=digest({'plan':record.get('plan'),'stage':stage,'completed_stages':record.get('completed_stages',[])},length=64))
                    lease=self.connection.execute('SELECT owner,expires FROM leases WHERE experiment_id=?',(experiment_id,)).fetchone()
                    if lease:entry.update(lease_owner=lease[0],lease_expiry=lease[1])
                if status in {'succeeded','failed'}:
                    entry.update(ended_at=entry['updated_at'],output_digest=digest(updates,length=64))
                    if status=='failed':entry['error']=updates.get('stage_error','')
                if status=='succeeded':
                    completed=record.setdefault('completed_stages',[])
                    if stage not in completed: completed.append(stage)
            self.connection.execute("UPDATE experiments SET payload=?,updated_at=? WHERE id=?", (json.dumps(record,sort_keys=True),datetime.now(timezone.utc).isoformat(),experiment_id))
            # The record retains every attempt. Events reference checkpoints;
            # copying the growing trial array into every event is quadratic.
            summary={'stage':stage,'stage_status':status,
                'checkpoint_digest':__import__('atsuite.recommender.identity',fromlist=['digest']).digest(updates,length=64)}
            for key in ('trials','resource_records','deployments','build_outputs','search_audit','child_candidates'):
                if key in updates:summary[key+'_count']=len(updates[key])
            for key in ('error','stage_error','cleanup_error','cleanup_status','decision_status','report_path','usage_ledger','report_digest'):
                if key in updates:summary[key]=updates[key]
            self._append_event(experiment_id,summary)
            return record

    def register_approval(self, approval):
        with self._transaction():
            self.connection.execute("INSERT INTO approvals VALUES (?,?)", (approval['approval_id'],json.dumps(approval,sort_keys=True)))
        return approval

    def approval(self, approval_id):
        with self._lock:
            row=self.connection.execute("SELECT payload FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if not row: raise ValueError('unknown approval')
            return json.loads(row[0])

    def queued(self):
        with self._lock:
            return [r[0] for r in self.connection.execute("SELECT id FROM experiments WHERE state IN ('APPROVED','PREFLIGHT','BUILDING','DEPLOYING','EXPLORING','CONFIRMING','ANALYZING','CLEANUP_PENDING','CANCEL_REQUESTED') ORDER BY updated_at")]

    def events(self, experiment_id: str, after: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute("SELECT seq, event, payload FROM events WHERE experiment_id=? AND seq>? ORDER BY seq", (experiment_id, after)).fetchall()
            return [{"id": seq, "event": event, **json.loads(payload)} for seq, event, payload in rows]

    def claim_idempotency(self, scope: str, key: str, request_hash: str, resource_id: str) -> str:
        with self._transaction():
            row = self.connection.execute("SELECT request_hash, resource_id FROM idempotency WHERE scope=? AND key=?", (scope, key)).fetchone()
            if row:
                if row[0] != request_hash:
                    raise ValueError("idempotency key was already used with a different request")
                return str(row[1])
            self.connection.execute("INSERT INTO idempotency VALUES (?, ?, ?, ?)", (scope, key, request_hash, resource_id))
            return resource_id

    def register_artifact(self, artifact_id: str, tenant: str, project: str, relative_path: str, media_type: str = "application/json") -> None:
        with self._lock:
            self.connection.execute("INSERT OR REPLACE INTO artifacts VALUES (?, ?, ?, ?, ?)", (artifact_id, tenant, project, relative_path, media_type))
            self.connection.commit()

    def artifact(self, artifact_id: str, tenant: str, project: str) -> dict[str, str]:
        with self._lock:
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
        with self._lock:
            row = self.connection.execute("SELECT payload FROM benchmarks WHERE id=? AND tenant=? AND project=?", (benchmark_id, tenant, project)).fetchone()
            if row is None:
                raise KeyError(benchmark_id)
            return json.loads(row[0])

    def close(self):
        self.connection.close()
