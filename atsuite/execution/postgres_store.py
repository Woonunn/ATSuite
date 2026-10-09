"""Production transactional store using PostgreSQL across control plane/worker hosts.

The canonical JobStore state/approval/checkpoint policy is reused. Short database
transactions acquire a deployment-specific advisory lock; cloud calls are always
outside those transactions. SQLite remains the development store.
"""
from __future__ import annotations

import contextlib
import threading
from atsuite.execution.store import JobStore

_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS experiments (id TEXT PRIMARY KEY, state TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS events (experiment_id TEXT NOT NULL, seq BIGINT NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(experiment_id, seq))',
    'CREATE TABLE IF NOT EXISTS idempotency (scope TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL, resource_id TEXT NOT NULL, PRIMARY KEY(scope, key))',
    'CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, tenant TEXT NOT NULL, project TEXT NOT NULL, relative_path TEXT NOT NULL, media_type TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS benchmarks (id TEXT PRIMARY KEY, tenant TEXT NOT NULL, project TEXT NOT NULL, payload TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS leases (experiment_id TEXT PRIMARY KEY, owner TEXT, token TEXT, expires DOUBLE PRECISION)',
    'CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, payload TEXT NOT NULL)',
    "CREATE INDEX IF NOT EXISTS experiments_queue_idx ON experiments (updated_at, id) WHERE state IN ('APPROVED','PREFLIGHT','BUILDING','DEPLOYING','EXPLORING','CONFIRMING','ANALYZING','CLEANUP_PENDING','CANCEL_REQUESTED')",
)
_UPSERTS = {
    'INSERT OR REPLACE INTO leases VALUES (?,?,?,?)':
        'INSERT INTO leases VALUES (%s,%s,%s,%s) ON CONFLICT (experiment_id) DO UPDATE SET owner=EXCLUDED.owner,token=EXCLUDED.token,expires=EXCLUDED.expires',
    'INSERT OR REPLACE INTO artifacts VALUES (?, ?, ?, ?, ?)':
        'INSERT INTO artifacts VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET tenant=EXCLUDED.tenant,project=EXCLUDED.project,relative_path=EXCLUDED.relative_path,media_type=EXCLUDED.media_type',
}


def postgres_sql(sql: str) -> str:
    if sql in _UPSERTS:
        return _UPSERTS[sql]
    if 'OR REPLACE' in sql or 'BEGIN IMMEDIATE' in sql:
        raise ValueError('unsupported SQLite-specific SQL in PostgreSQL store')
    return sql.replace('?', '%s')


class _Connection:
    def __init__(self, raw): self.raw = raw
    def execute(self, sql, parameters=()): return self.raw.execute(postgres_sql(sql), parameters)
    def commit(self): self.raw.commit()
    def rollback(self): self.raw.rollback()
    def close(self): self.raw.close()


class PostgresJobStore(JobStore):
    def __init__(self, dsn: str, *, connector=None):
        if not dsn.startswith(('postgresql://', 'postgres://')):
            raise ValueError('PostgreSQL DSN required')
        if connector is None:
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError('PostgreSQL store requires uv sync --group production') from exc
            connector = psycopg.connect
        # DSN can contain credentials. Do not expose it as a repr/path/log value.
        self.path = 'postgresql://configured'
        self._lock = threading.RLock()
        self.connection = _Connection(connector(dsn, autocommit=True))
        for sql in _SCHEMA:
            self.connection.execute(sql)

    @contextlib.contextmanager
    def _transaction(self):
        with self._lock:
            # One shared database is one ATSuite deployment. Advisory locking
            # preserves canonical read-modify-write and event sequence policy
            # across independent processes without holding a cloud-operation lock.
            with self.connection.raw.transaction():
                self.connection.execute("SET LOCAL statement_timeout = '30s'")
                self.connection.execute("SET LOCAL lock_timeout = '15s'")
                self.connection.execute('SELECT pg_advisory_xact_lock(%s)', (0x41545355495445,))
                yield


def open_store(path=':memory:'):
    if str(path).startswith(('postgresql://', 'postgres://')):
        return PostgresJobStore(str(path))
    return JobStore(path)
