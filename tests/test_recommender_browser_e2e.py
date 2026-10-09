"""Real Chromium/UI/API/worker tests. External execution transport is simulated.

Run: ATSUITE_BROWSER_E2E=1 PLAYWRIGHT_BROWSERS_PATH=/private/tmp/atsuite-playwright
     uv run --group dev python -m unittest tests.test_recommender_browser_e2e -v
"""
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path


@unittest.skipUnless(os.environ.get('ATSUITE_BROWSER_E2E') == '1', 'explicit real browser E2E opt-in')
class BrowserWorkflowTests(unittest.TestCase):
    def test_upload_plan_approval_queue_polling_worker_evidence_and_exports(self):
        import uvicorn
        from playwright.sync_api import sync_playwright, expect
        from atsuite.server.app import create_app
        from atsuite.recommender import service
        from tests.fake_execution_backend import FakeExecutionBackend
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(store_path=root/'jobs.sqlite', artifact_root=root/'artifacts', execution_backend=FakeExecutionBackend())
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            for _ in range(100):
                if server.started: break
                time.sleep(.05)
            self.assertTrue(server.started)
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page(accept_downloads=True)
                    page.goto(f'http://127.0.0.1:{port}')
                    expect(page.locator('#principal')).to_have_value('local-user / local / default / editor')
                    archive = self._archive(root)
                    page.locator('#benchmarkFile').set_input_files(str(archive))
                    expect(page.locator('#uploadStatus')).to_contain_text('files')
                    page.locator('#regions').fill('aws_lambda=us-east-1')
                    page.locator('#objective').select_option('min_cost')
                    page.locator('#latencySlo').fill('')
                    page.locator('#successRate').fill('0')
                    page.locator('#candidateLimit').fill('2')
                    page.locator('#prediction').uncheck()
                    page.locator('#advisor').uncheck()
                    page.locator('button[type=submit]').click()
                    expect(page.locator('#view-plan')).to_be_visible()
                    self.assertIsNone(page.evaluate('window.__xss'))
                    page.locator('[data-view=new]').click()
                    page.locator('#createExperiment').click()
                    expect(page.locator('#toast')).to_contain_text('已冻结')
                    experiment_id = page.evaluate("localStorage.getItem('atsuite.experiment')")
                    page.locator('#approveExperiment').click()
                    expect(page.locator('#toast')).to_contain_text('已批准')
                    page.locator('#runExperiment').click()
                    expect(page.locator('#workerStatus')).to_have_text('queued_waiting_for_worker')
                    # Drop the first event poll. The browser keeps its cursor and
                    # reconnects against the real API, rather than inventing progress.
                    page.route('**/events?*', lambda route: route.abort(), times=1)
                    page.locator('#refreshProgress').click()
                    expect(page.locator('#workerStatus')).to_contain_text('重试')
                    expect(page.locator('#workerStatus')).to_have_text('queued_waiting_for_worker', timeout=10000)
                    worker = threading.Thread(target=lambda: service.run(app.state.store, experiment_id, backend=app.state.execution_backend), daemon=True)
                    worker.start()
                    worker.join(timeout=300)
                    self.assertFalse(worker.is_alive())
                    record = app.state.store.get(experiment_id)
                    self.assertEqual(record['state'], 'COMPLETED', {k: record.get(k) for k in ('error','cleanup_error','state','stage_records')})
                    expect(page.locator('#workerStatus')).to_have_text('COMPLETED', timeout=15000)
                    page.locator('[data-view=compare]').click()
                    page.locator('#loadComparison').click()
                    expect(page.locator('#comparisonRows')).not_to_contain_text('等待后端')
                    page.locator('[data-view=recommend]').click()
                    page.locator('#loadRecommendation').click()
                    expect(page.locator('#qualificationOutput')).to_contain_text('eligibility')
                    self.assertEqual(len([t for t in record['trials'] if t['phase']=='confirmation']), 600)
                    self.assertIsNone(record['recommendation']['best_verified'])
                    self.assertEqual(record['recommendation']['status'], 'no_clear_winner')
                    expect(page.locator('#winnerCard')).to_contain_text('no_clear_winner')
                    self.assertFalse(record['comparison']['cloud_verified'])
                    page.locator('#evidenceRefs button').first.click()
                    expect(page.locator('#evidenceOutput')).to_contain_text('candidate_id')
                    self.assertIsNone(page.evaluate('window.__xss'))
                    with page.expect_download() as info:
                        page.locator('#exportExperiment').click()
                    exported = json.loads(Path(info.value.path()).read_text())
                    self.assertEqual(exported['id'], experiment_id)
                    with page.expect_download() as info:
                        page.locator('#exportMarkdown').click()
                    self.assertIn('ATSuite experiment', Path(info.value.path()).read_text())
                    page.locator('#repriceExperiment').click()
                    expect(page.locator('#repriceVersion option')).to_have_count(2)
                    page.locator('#repriceVersion').select_option(index=1)
                    expect(page.locator('#repriceOutput')).not_to_have_text('')
                    page.locator('[data-view=sources]').click()
                    page.locator('#loadCapabilities').click()
                    expect(page.locator('#sourceCards')).to_contain_text('market_snapshot')
                    # Failure of credentials is displayed; forged role headers
                    # are never emitted by the browser.
                    page.locator('#apiToken').fill('invalid')
                    # Local mode uses no configured token, so test protected mode
                    # separately below instead of claiming this is auth verification.
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)

    def test_browser_cancellation_failed_cleanup_and_resume(self):
        import uvicorn
        from playwright.sync_api import sync_playwright, expect
        from atsuite.server.app import create_app
        from atsuite.recommender import service
        from tests.fake_execution_backend import FakeExecutionBackend
        deployed, release = threading.Event(), threading.Event()
        class PausedTransport(FakeExecutionBackend):
            def deploy(self, *args):
                result = super().deploy(*args)
                deployed.set()
                if not release.wait(60):
                    raise TimeoutError('browser did not cancel while deployment paused')
                return result
        backend = PausedTransport(fail_cleanup=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(store_path=root/'jobs.sqlite', artifact_root=root/'artifacts', execution_backend=backend)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
            thread = threading.Thread(target=server.run, daemon=True); thread.start()
            for _ in range(100):
                if server.started: break
                time.sleep(.05)
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page()
                    page.goto(f'http://127.0.0.1:{port}')
                    page.locator('#benchmarkFile').set_input_files(str(self._archive(root)))
                    expect(page.locator('#uploadStatus')).to_contain_text('files')
                    page.locator('#regions').fill('aws_lambda=us-east-1')
                    page.locator('#candidateLimit').fill('2')
                    page.locator('#prediction').uncheck()
                    page.locator('#advisor').uncheck()
                    page.locator('#createExperiment').click()
                    expect(page.locator('#toast')).to_contain_text('已冻结')
                    experiment_id = page.evaluate("localStorage.getItem('atsuite.experiment')")
                    page.locator('#approveExperiment').click()
                    expect(page.locator('#toast')).to_contain_text('已批准')
                    page.locator('#runExperiment').click()
                    expect(page.locator('#workerStatus')).to_have_text('queued_waiting_for_worker')
                    worker = threading.Thread(target=lambda: service.run(app.state.store, experiment_id, backend=backend), daemon=True)
                    worker.start()
                    self.assertTrue(deployed.wait(30))
                    page.locator('#cancelExperiment').click()
                    expect(page.locator('#workerStatus')).to_have_text('CANCEL_REQUESTED')
                    release.set()
                    worker.join(timeout=30)
                    self.assertFalse(worker.is_alive())
                    expect(page.locator('#workerStatus')).to_have_text('FAILED', timeout=10000)
                    failed = app.state.store.get(experiment_id)
                    self.assertEqual(failed['cleanup_status'], 'failed')
                    self.assertIn('simulated deletion denied', failed['cleanup_error'])
                    self.assertFalse(any(call[0] == 'invoke' for call in backend.calls if isinstance(call, tuple)))
                    page.locator('#resumeExperiment').click()
                    expect(page.locator('#workerStatus')).to_have_text('CLEANUP_PENDING')
                    backend.fail_cleanup = False
                    service.run(app.state.store, experiment_id, backend=backend)
                    expect(page.locator('#workerStatus')).to_have_text('CANCELLED', timeout=10000)
                    final = app.state.store.get(experiment_id)
                    self.assertEqual(final['cleanup_status'], 'success')
                    self.assertEqual(sum(call == 'publish' for call in backend.calls), 1)
                    self.assertIsNone(page.evaluate('window.__xss'))
                    browser.close()
            finally:
                release.set(); server.should_exit = True; thread.join(timeout=10)

    @staticmethod
    def _archive(root):
        malicious = '<img src=x onerror=window.__xss=1>'
        archive = root/'benchmark.zip'
        config = {'trace_file': './trace/workflow.json',
                  'nodes': [{'name': malicious, 'dir': './nodes/tool', 'trace_names': ['tool.run'], 'state': {'mode': 'stateless'}}],
                  'pipeline': {'faas': {'units': [{'name': 'tool', 'node': malicious, 'trace_names': ['tool.run'],
                                                   'deploy': {'memory': 1769, 'disk': 512, 'timeout': 30}}]}},
                  'correctness': {'oracle': {'type': 'exact', 'expected_output': {'answer': 42}}}}
        trace = {'nodes': [{'id': 0, 'name': 'start', 'type': 'logic', 'edge_to': [{'id': 1, 'params': {}}]},
                           {'id': 1, 'name': 'tool.run', 'type': 'tool_use', 'edge_to': []}]}
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('benchmark/config/demo.json', json.dumps(config))
            bundle.writestr('benchmark/trace/workflow.json', json.dumps(trace))
            bundle.writestr('benchmark/nodes/tool/implementation.py', 'from atsuite_sdk.abstract import registry\n@registry.tool()\ndef run():\n    return {"answer": 42}\n')
            bundle.writestr('benchmark/nodes/tool/requirements.txt', '')
        return archive

    def test_browser_reports_real_authentication_failure(self):
        import uvicorn
        from playwright.sync_api import sync_playwright, expect
        from atsuite.server.app import create_app
        with tempfile.TemporaryDirectory() as tmp:
            token = 'secure-browser-token-' + 'a' * 32
            app = create_app(artifact_root=tmp, identity_tokens={token: {
                'subject': 'viewer', 'tenant': 'tenant', 'project': 'project', 'role': 'viewer'}})
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
            thread = threading.Thread(target=server.run, daemon=True); thread.start()
            for _ in range(100):
                if server.started: break
                time.sleep(.05)
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page()
                    page.goto(f'http://127.0.0.1:{port}')
                    expect(page.locator('#principal')).to_have_value('401 invalid authorization')
                    page.locator('[data-view=sources]').click()
                    page.locator('#apiToken').fill(token)
                    page.locator('#saveSettings').click()
                    expect(page.locator('#principal')).to_have_value('viewer / tenant / project / viewer')
                    self.assertTrue(page.locator('#productionApply').is_disabled())
                    browser.close()
            finally:
                server.should_exit = True; thread.join(timeout=10)
