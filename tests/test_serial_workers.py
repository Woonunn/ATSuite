import threading
import unittest
from atsuite.execution.workers import run_parallel


class SerialWorkerTests(unittest.TestCase):
    def test_one_worker_runs_on_calling_thread_in_order(self):
        caller = threading.get_ident()
        self.assertEqual(run_parallel([2, 1], lambda n: (n, threading.get_ident()), max_workers=1),
                         [(2, caller), (1, caller)])

    def test_serial_failure_does_not_dispatch_remaining_cloud_work(self):
        calls = []
        def worker(value):
            calls.append(value)
            raise RuntimeError('failed')
        with self.assertRaisesRegex(RuntimeError, 'failed'):
            run_parallel([1, 2], worker, max_workers=1)
        self.assertEqual(calls, [1])
