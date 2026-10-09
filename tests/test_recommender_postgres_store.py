import os
import unittest
from contextlib import contextmanager
from unittest.mock import Mock

from atsuite.execution.postgres_store import PostgresJobStore, postgres_sql, open_store


class PostgresStoreContractTests(unittest.TestCase):
    def test_upserts_placeholders_schema_and_transaction_lock(self):
        connection = Mock()
        connection.transaction.side_effect = contextmanager(lambda: (yield))
        store = PostgresJobStore('postgresql://secret@localhost/atsuite', connector=lambda *a, **kw: connection)
        self.assertNotIn('secret', store.path)
        sqls = [call.args[0] for call in connection.execute.call_args_list]
        self.assertTrue(any('DOUBLE PRECISION' in sql for sql in sqls))
        self.assertTrue(any('experiments_queue_idx' in sql for sql in sqls))
        with store._transaction():
            store.connection.execute('SELECT token FROM leases WHERE experiment_id=?', ('exp',))
        sqls = [call.args[0] for call in connection.execute.call_args_list]
        self.assertTrue(any('pg_advisory_xact_lock' in sql for sql in sqls))
        self.assertEqual(sqls[-1], 'SELECT token FROM leases WHERE experiment_id=%s')
        self.assertIn('ON CONFLICT (experiment_id) DO UPDATE', postgres_sql('INSERT OR REPLACE INTO leases VALUES (?,?,?,?)'))
        with self.assertRaises(ValueError):
            postgres_sql('INSERT OR REPLACE INTO unexpected VALUES (?)')

    def test_sqlite_factory_preserves_default_local_execution(self):
        store = open_store()
        record = store.create('local', {}, state='APPROVED')
        token = store.claim('local', 'worker')
        self.assertIsNotNone(token)
        self.assertEqual(store.get('local'), record)
        store.close()

    @unittest.skipUnless(os.environ.get('ATSUITE_TEST_POSTGRES_DSN'), 'PostgreSQL service not configured')
    def test_live_independent_connections_fence_workers(self):
        import uuid
        dsn = os.environ['ATSUITE_TEST_POSTGRES_DSN']
        first, second = PostgresJobStore(dsn), PostgresJobStore(dsn)
        identity = 'contract-' + uuid.uuid4().hex
        try:
            first.create(identity, {'tenant': 'test', 'project': 'test'}, state='APPROVED')
            token = first.claim(identity, 'worker-a')
            self.assertIsNotNone(token)
            self.assertIsNone(second.claim(identity, 'worker-b'))
            first.checkpoint(identity, 'PREFLIGHT', {'complete': True}, lease_token=token)
            self.assertTrue(second.get(identity)['complete'])
            with self.assertRaises(RuntimeError):
                second.checkpoint(identity, 'BUILDING', {}, lease_token='forged')
            first.release(identity, token)
            self.assertIsNotNone(second.claim(identity, 'worker-b'))
        finally:
            for table in ('events', 'leases'):
                first.connection.execute(f'DELETE FROM {table} WHERE experiment_id=%s', (identity,))
            first.connection.execute('DELETE FROM experiments WHERE id=%s', (identity,))
            first.close(); second.close()
