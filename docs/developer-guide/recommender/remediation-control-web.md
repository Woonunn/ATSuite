# FIX-10 / FIX-11 control plane and browser remediation

This document records implementation and executed checks, independently from external cloud acceptance. Start HEAD is recorded in remediation-status.md. No cloud credentials or request bodies are copied into audit records.

## Reproduction

The original `HEAD:atsuite/server/app.py` was extracted read-only into `/private/tmp/atsuite-original-server.py` and run against the current project environment. A request with forged `X-ATSuite-Role: admin`, traversal tenant, and `benchmark.config=/etc/passwd` reached server file parsing (400 JSON parse error). The original security module published an archive containing a nested `trace_file=../../../outside.json`; `destination.exists()` was True. These are current-checkout reproductions, not results copied from the review attachment.

Initial regression import was blocked until the project's locked FastAPI dependencies finished installing. The nested-reference regression then failed against the original extractor; the fixed module rejects the reference and leaves no destination. New API regressions exercise authorization, path rejection, transactional ZIP publication, streamed upload limits, scoped tokens, expiry/audience, production startup, ownership/drift, and failed health rollback.

## Implementation

- `atsuite/server/auth.py`: server configured opaque token → immutable principal, explicit audience/expiry, fixed subject/tenant/project/role. Client identity headers have no authority. Local unauthenticated development is restricted to loopback and cannot grant production admin.
- `atsuite/server/security.py`: streamed/declared upload limits, entry count/size/ratio/type/case collision checks, private staging, all JSON nested reference confinement, atomic publication and failed-upload cleanup.
- `atsuite/server/app.py`: benchmark/config/artifact IDs, ownership checks, approval separate from intent, canonical service plan/approval/enqueue/resume, audit, recoverable cursor polling, actual record comparison/recommendation/sources, versioned repricing, full JSON/Markdown export.
- `atsuite/server/production.py`: explicit controlled adapter transaction; verified winner and exact configuration, ownership, pre-change snapshot, separate approval, drift check, actual apply, health check, rollback and readback. Missing adapters fail explicitly, never return fake queued success.
- `web/recommender`: textContent based dynamic rendering, service-principal permissions, session scoped token, frozen task approval, real queue/waiting status, retry with event cursor, evidence provenance, repricing versions, exports, controlled production change workflow.
- `atsuite/cli/serve.py`: development loopback binding restriction; production fails without configured identity and execution isolation capabilities.

## Executed local checks

`UV_CACHE_DIR=/private/tmp/atsuite-uv-cache uv run --locked --group dev python -m unittest tests.test_recommender_api_authorization -v`: **8 passed**. The command ran outside the sandbox after the sandboxed uv invocation panicked while accessing macOS system network configuration. It still uses this project's locked dependency environment.

`UV_CACHE_DIR=/private/tmp/atsuite-uv-cache uv run --no-sync python -m unittest tests.test_recommender_api_authorization tests.test_recommender_platform.OptionalIntegrationTests.test_control_plane_idempotency_origin_tenant_and_artifacts -v`: **9 passed** in the same synchronized project environment. Existing positive assertions remain; their identity fixture now uses server configured principals and uploaded IDs.

Playwright **1.58.0** is declared in the dev group and uv.lock. Real Chromium v1208 from the preinstalled exact Playwright cache completed three browser E2E tests. The separate temporary Chromium download had a CDN connection reset; no browser proof depends on that download.

## Acceptance state

| Task | implemented | unit_tested | integration_tested | browser_e2e_tested | cloud_verified |
|---|---|---|---|---|---|
| FIX-10 | local implementation | passed | local API passed | passed (real Chromium) | blocked_external |
| FIX-11 | local implementation | API passed | local API passed | passed (real Chromium) | blocked_external |

Production provider apply adapters and live identity-provider deployment are externally configured capabilities. Only the ownership/drift/health/rollback contract is locally proven; no live production change has been performed. Browser external transport simulations will be labeled simulated and cannot prove cloud verification.

## Final executed browser result

`ATSUITE_BROWSER_E2E=1 UV_CACHE_DIR=/private/tmp/atsuite-uv-cache uv run --locked --group dev python -m unittest tests.test_recommender_browser_e2e -v`: **3 passed, 107.962 seconds**. Detailed run log: `/private/tmp/atsuite-browser-e2e.log`; compact repository evidence: `remediation-control-web-results.txt`.

The three actual Chromium tests exercise configured-token authentication failure and viewer permissions; upload/preview/frozen task/explicit approval/queue; network-aborted progress polling with cursor recovery; real default handlers and actual DAG execution through only simulated external transport; **614 recorded attempts, including 600 independent confirmation attempts**; deterministic derived-cost `no_clear_winner`; evidence hash/ownership resolution; full JSON and Markdown downloads; repricing version selection; source records; malicious node-name safe rendering; cancellation during resource creation; actual failed cleanup and successful resumed cleanup without invocations or rebuilding.

Simulated transport remains labeled simulated, and the browser test asserts `best_verified is None` and `cloud_verified is False`. This is product execution proof, not cloud validation.

Failed browser runs exposed and corrected shared SQLite concurrent access, quadratic progress event payloads, normalized frozen-file inventory mismatches, and late async responses overriding selected navigation. No semantic assertions or sample counts were removed. The bounded worker wait increased from 60 to 300 seconds to accommodate the complete confirmation protocol; the final run completes all 600 confirmation attempts.

## PostgreSQL production store

`execution/postgres_store.py` reuses canonical JobStore state, lease fencing, checkpoints, budget and approval methods. `open_store` selects SQLite for local paths and PostgreSQL for `postgresql://` or `postgres://`. Psycopg **3.3.3** and its binary wheel are pinned in the production dependency group and uv.lock. Database DSNs are not retained in logs or the store's public path.

Short transactions take one deployment-level PostgreSQL advisory lock, so distinct control plane and worker processes serialize state updates and event IDs correctly. Cloud operations occur outside these transactions. This conservative implementation serializes database mutations; it does not claim per-experiment write scaling. Queue lookup has a partial index, and lease timestamps use double precision.

`uv run --no-sync python -m unittest tests.test_recommender_postgres_store -v`: **2 SQL/transaction contract tests passed; 1 live PostgreSQL fencing test skipped**. No `postgres`, `initdb`, `pg_ctl` or `psql` binary or PostgreSQL DSN was available. Live database validation remains `blocked_external`; it can run with `ATSUITE_TEST_POSTGRES_DSN` and `uv run --group production`. Contract tests do not claim database-server concurrency validation.

Identity migration: legacy X-ATSuite-Role/Tenant/Project headers and ATSUITE_API_TOKEN no longer grant authority. Configure `ATSUITE_IDENTITY_TOKENS` with server-owned opaque tokens mapped to subject/tenant/project/role, optionally audience and expires_at. Production startup derives builder and cleanup capabilities from the configured backend, and refuses simulated or unconfigured isolation. The built-in AWS Lambda production adapter reads server-approved `ATSUITE_PRODUCTION_TARGETS`; no apply target or handler module is accepted from caller configuration.

Latest targeted rerun after built-in Lambda SDK contract and request failure auditing: **15 tests run, 14 passed, 1 PostgreSQL live test skipped**, 0.327 seconds (see command in compact results). Production adapter contract asserts allowlisted function identity, immutable image, config readback, health oracle and exact snapshot restoration; no real cloud API was called.

Final combined verification on the root's locked all-groups environment:
`ATSUITE_BROWSER_E2E=1 uv run --locked --all-groups python -m unittest discover -s tests -p 'test_*.py' -v`
ran **241 tests in 132.804 seconds, 240 passed and 1 live PostgreSQL test skipped**.
All three Chromium E2E flows passed; [evidence/final-suite.txt](evidence/final-suite.txt)
contains the full combined result. Before this combined run, a failed two-version
assertion exposed redundant serial report reloads after repricing; the final UI renders
and selects the returned version directly, rejects older reads, and retains all
semantic assertions. See reprice-browser-before.txt for the earlier failure.

The additional strengthened browser flow passed in **108.792 seconds**:
automatic new-version selection and unchanged original report digest are now
asserted explicitly. Output: [evidence/reprice-browser-after.txt](evidence/reprice-browser-after.txt).
