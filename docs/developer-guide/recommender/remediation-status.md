# Recommender remediation

Started 2026-10-08; updated 2026-10-09. Branch `feat/recommender`.
Start HEAD `de752f21342162dd4412ea9dd3b6e224a1f633fd`, matching the reviewed commit.
Initial `git status --short` was empty. AGENTS.md, the implementation record,
user guide, requested remediation specification and review probes were read.
No branch creation/switch, reset, rebase, stash, user-change overwrite or push.

## Baseline and reproduction

`uv sync --locked` initially failed on sandbox access to uv's user cache. The
approved retry installed the actual locked project environment. A read-only
archive of the original HEAD was tested against that project environment:
**93 original tests passed in 8.666s**, saved in
[evidence/locked-baseline.txt](evidence/locked-baseline.txt).
The earlier incomplete environment attempt is retained separately in baseline.txt
and is not the baseline result. The dependency lock subsequently gained declared
Playwright dev and psycopg production dependencies.

The four attachment test intentions were moved to production-module regressions;
the simplified attachment contracts did not replace repository implementations.
[evidence/review-before.txt](evidence/review-before.txt) records all four original
failures. Tests verify decision.__file__ resolves inside this checkout.
Additional pre-fix failures cover resume/leases, source identity/digests, absent
default handlers, topology/resources, pricing, feature encoding, advisor redaction,
small-sample statistics, untracked bundle injection and offline export comparison.
Original and intermediate failures remain under evidence/; they are not presented
as final pass reports. Final verification commands and results are recorded below.

## Acceptance matrix

`local_contracts` includes explicitly simulated external cloud/registry transports;
it never means real cloud deployment or held-out model quality was verified.
Each status below is independent of the others.

| Fix | implemented | unit_tested | integration_tested | browser_e2e_tested | cloud_verified | model_evaluated |
| --- | --- | --- | --- | --- | --- | --- |
| FIX-00 | yes | passed | original locked baseline + four real regressions | passed | blocked_external | n/a |
| FIX-01 | yes | passed | shared gate + positive/negative + export replay | passed | blocked_external | n/a |
| FIX-02 | yes | passed | frozen bundles + real SDK oracle/state transactions | n/a | blocked_external | n/a |
| FIX-03 | yes | passed | six adapter/capability contracts | n/a | blocked_external | n/a |
| FIX-04 | yes | passed | build/publish/deploy/readback contracts | n/a | blocked_external | n/a |
| FIX-05 | yes | passed | default cleanup recovery + two-store fencing + budget/TTL | passed | blocked_external | n/a |
| FIX-06 | yes | passed | meter fixtures + ledger tiers/rounding + immutable reprice | passed | blocked_external | n/a |
| FIX-07 | yes | passed | actual CLI→default worker→DAG/oracle/ledger/cleanup | passed | blocked_external | n/a |
| FIX-08 | yes | passed | acquisition ordering + actual fallback | n/a | n/a | insufficient_real_data |
| FIX-09 | yes | passed | immutable child execution + real grid fallback + explanation contracts | n/a | blocked_external | blocked_external: real LLM API |
| FIX-10 | yes | passed | auth/upload/production change contracts | passed | blocked_external | n/a |
| FIX-11 | yes | passed | actual API/queue/poll/export/reprice | passed | blocked_external | n/a |
| FIX-12 | yes: local work | passed | default chain + six-service opt-in entry contracts | passed | blocked_external | insufficient_real_data / blocked_external |

## Difference audit and implementation records

| Fix | Problem/root cause and result | Files/interfaces and evidence |
| --- | --- | --- |
| FIX-00 | Four review defects reproduced at current SHA; request fields/legacy paths no longer authorize writes. Behavior changed intentionally. | test_recommender_review_regressions.py, shared approval service, CLI migration below; review-before.txt. |
| FIX-01 | Old gate ignored counts, oracle, constraint metrics/currencies and arbitrary refs; old engine published independently. Fixed with one versioned gate and canonical ranking. | eligibility.py, decision.py, evidence_adapter.py, statistics.py, engine.py; statistics-before/after.txt. Exact one-sided binomial bound, block quantile uncertainty, deadline lower bound, cost ratio intervals, simultaneous confidence, fixed practical units/relative gap. |
| FIX-02 | Invalid grouping still compiled; missing state split; first family baseline reused; bundle data lost. Fixed conservative atoms, compiler revalidation, full frozen bundles and stable state-domain routing. | topology.py, api.py, StateKey, SDK/state/seed/cleanup; remediation-planning.md. |
| FIX-03 | Sparse sampled resources were treated as legality; homogeneous tuning lost targets and later providers. Fixed exact baseline, legal rules vs samples, coordinate search and round-robin coverage. | capabilities.py, resources.py, pipeline.py, actual adapters; provider_options/resource_search contracts. |
| FIX-04 | Build key missed inputs, fresh local images could not publish, physical resources collided and effective fields were invented. Fixed source/SDK/data/lock/base digest identity, registry publication, namespace and actual readback. | execution/base_images.py, publish.py, build.py, safe_process.py, readback.py, deploy.py + adapters; artifacts-before.txt, build-after.txt. |
| FIX-05 | Resume reset to BUILDING, stage entry implied success, leases/budgets weak and partial deployments leaked. Fixed checkpoints, fencing/heartbeat, creation intents, cleanup recovery, durable reservations and TTL audit. | jobs.py, store.py, postgres_store.py, budget.py, cleanup.py; jobs-before.txt, budget-before/after.txt, resume tests. SQLite explicit local mode; PostgreSQL supported multi-host transactional path. |
| FIX-06 | Wrong meter match/dedup/rounding, missing amount zero, FX false dates and aggregate-only reprice. Fixed immutable attributed usage, Decimal full selector match/account tiers, source-date freshness and candidate re-ranking. | market_data/, Analyzer additive raw usage, cost statistics; remediation-market-models.md. |
| FIX-07 | Worker required user-written handlers; CLI/Web/offline differed. Fixed built-in real external adapters plus canonical service used everywhere. | default_handlers.py, cloud_backend.py, service.py, CLI/server; default-before.txt, default-tests.txt. Local fake boundary is explicitly simulated. |
| FIX-08 | Mapping features became zeros; provider/mode absent; predictor didn't alter next acquisition. Fixed frozen categorical/full resource features and pilot-based iterative selection with real fallback. | prediction/, exploration; prediction_acquisition contracts, model reports remain insufficient_real_data. |
| FIX-09 | Intent units lost, nested secrets retained, proposals only existing IDs and text could contradict evidence. Fixed tighter typed intent, bounded immutable children, token reservations and exact metric/winner verifier. | advisor/, execution child proposal integration, api.materialize_child_candidate; advisor_workflow tests and child tests. |
| FIX-10 | Client headers forged tenant/admin; server paths and nested ZIP refs escaped; uploaded init ran on host. Fixed server-owned principals, ID-only API, transactional streaming ZIP validation and credential-free limited builder. | server/auth.py, security.py, production.py, app.py, isolated_builder.py; remediation-control-web.md. Production apply is separate allowlisted snapshot/approval/health/rollback action. |
| FIX-11 | UI guessed progress, labels were disconnected and export/reprice incomplete. Fixed actual queue/evidence views, safe DOM, cursor recovery, immutable artifacts and compact checkpoint events retaining all trials. | web/recommender + server; real Chromium passed all three flows, including 600 confirmation attempts. |
| FIX-12 | Prior claims lacked default-handler/browser/live evidence. Added CLI/default-chain, real SDK state oracle, browser and opt-in six-service cloud acceptance tool; documentation now distinguishes implementation from validation. | tests/, benchmarks/RecommenderOracle/, tools/recommender_cloud_acceptance.py, updated user guide and this record. |

## Migration and operational limits

- Experiment schema remains 1; Analyzer remains 2; recommendation evidence is
  separately versioned 2. Older evidence is readable as legacy_unverified.
- Old --config CLI normalizes to the shared plan; cloud execution needs explicit
  --approve-budget, finite spend/wall budgets and matching approved accounts.
  Legacy custom scoring/keep-resources/bare FX options cannot bypass frozen
  verified policy. The legacy Python engine exposes descriptive records, and
  only canonical qualified evidence can publish its winner.
- --handlers is optional. An administrator extension needs --allow-custom-handlers;
  Web uploads cannot select Python factories.
- X-ATSuite-Role/Tenant/Project no longer authorize requests. Opaque principals
  are server configured; development binds loopback and production fails closed.
- Missing cost is null/unavailable. The prior analyzer-disabled test's zero-price
  expectation was replaced by explicit null **and unavailable status** assertions;
  no measured-price assertion was relaxed. Canonical positive engine fixtures
  now provide 300 actual test trials rather than pretending five samples prove P95.
- A min_latency objective freezes a default min_success_rate=0.95 when none was
  supplied; explicit stronger or weaker thresholds remain visible in the plan.
  Balanced scores include simultaneous failure-rate bounds, rather than using
  observed failures as a certain score contribution. Conflicting metric/source
  labels are rejected even when the row claims platform_observed.
- Stale snapshots require typed stale_policy=allow; their original dates and
  frozen_stale_approved status survive. Stale priced usage remains ineligible for
  cost ranking, rather than being relabeled available. Reject/default policies stop.
- Real asynchronous deletes remain CLEANUP_PENDING and stay in the polling queue
  until a not-found readback. Actual errors become FAILED and resume only cleanup.
- Budget reservation is a conservative dispatch guard; delayed billing and
  in-flight calls remain pending, and cleanup has reserved capacity.
- Real Docker daemon unavailable even after approved unsandboxed info read;
  container/registry live checks are blocked_local_environment. Cloud contracts
  were exercised with explicit fakes, not substituted cloud validation.
- No live cloud writes, paid model calls, production apply or remote push occurred.
  PostgreSQL server binaries/DSN are absent; SQL/transaction contracts pass but
  live multi-connection validation is blocked_external.
- Online catalogs may lack ancillary build/registry/network/log/session bounds;
  preflight stops unknown estimates. Approved frozen snapshot bounds are supported
  without a Python mapper. Missing coverage stays missing.
- AWS AgentCore actual managed mode and Alibaba's current ali_agentrun adapter
  (FC MCP SESSION_EXCLUSIVE, not native AgentRun) are identified explicitly.

## External service acceptance

Use the opt-in built-in chain; ordinary tests never enable paid operations:

```bash
uv run python -m tools.recommender_cloud_acceptance \
  --config benchmarks/RecommenderOracle/config/benchmark.json \
  --provider aws_lambda --region us-east-1 --account ACCOUNT_ID \
  --market-snapshot approved-market.json --max-spend 10 \
  --max-wall-seconds 3600 --output results/acceptance/aws-lambda
```

Without --confirm-live this only freezes a plan. An explicitly approved live run
adds that flag and retains registry/readback/trial/pricing/cleanup artifacts.
Run each service separately: aws_lambda, aws_agentcore, gcp_faas, gcp_mcp,
ali_fc and the actual FC MCP service behind ali_agentrun.
All six are currently **blocked_external**; no unseen-workload model evaluation or
real Advisor token-usage acceptance is claimed. LLM explanation is unnecessary
for deterministic reproduction of the winner.

| Actual service | live execution | region / mode verification |
| --- | --- | --- |
| AWS Lambda | blocked_external | not_run |
| AWS Bedrock AgentCore Runtime | blocked_external | not_run |
| GCP Cloud Run FaaS | blocked_external | not_run |
| GCP Cloud Run MCP | blocked_external | not_run |
| Alibaba FC FaaS | blocked_external | not_run |
| Alibaba FC MCP SESSION_EXCLUSIVE (`ali_agentrun` alias) | blocked_external | not_run |

Failure cleanup uses the same persisted ownership records and built-in worker:

```bash
uv run atsuite-recommend resume --experiment EXPERIMENT_ID --store results/acceptance/SERVICE/jobs.sqlite
uv run atsuite-worker --experiment EXPERIMENT_ID --store results/acceptance/SERVICE/jobs.sqlite
uv run atsuite-worker --reconcile-ttl --store results/acceptance/SERVICE/jobs.sqlite
```

The acceptance entry polls asynchronous cleanup within its approved wall-time
budget. One fully qualified managed-runtime candidate can verify that service;
this does not imply a comparative winner. Empty publication collections and
simulated observations cannot pass service acceptance.

## Commits and regression commands

All commits are local on feat/recommender. These are task records, not live-cloud
validation. Run each listed module with `uv run --locked --all-groups python -m
unittest MODULE -v`; the final discover command executes all modules together.
The full result, environment and limits apply to each row.

| Fix | Local commit | Regression module / result evidence |
| --- | --- | --- |
| FIX-00 | 17d2e73 | tests.test_recommender_review_regressions; review-before.txt, locked-baseline.txt |
| FIX-01 | c5d8587 | tests.test_recommender_evidence_eligibility, tests.test_recommender_statistical_contract, tests.test_recommender_memory_constraint; statistics-*, memory-*, source-stale-*, latency-success-*, balanced-failure-before.txt |
| FIX-02 | d89dd27 | tests.test_recommender_topology_validation, tests.test_recommender_state_domains, tests.test_recommender_oracle_benchmark; planning-regressions.txt |
| FIX-03 | d7b6bd9 | tests.test_recommender_resource_search, tests.test_recommender_provider_options; planning-regressions.txt |
| FIX-04 | b12b011 | tests.test_recommender_publication_isolation, tests.test_recommender_base_images; artifacts-before.txt, build-after.txt, base-images-after.txt |
| FIX-05 | 133c7f2, 7604bbf | tests.test_recommender_budget_cleanup_contract, tests.test_recommender_resume_cancel_cleanup; jobs-before.txt, budget-*, async-cleanup-before.txt, cloud-cleanup-after.txt |
| FIX-06 | 7b345b9 | tests.test_recommender_market_accounting, tests.test_recommender_agentcore_missing_usage; missing-usage-*, source-stale-* |
| FIX-07 | 60a55f4 | tests.test_recommender_default_execution, tests.test_recommend_cli; default-*, bundle-injection-before.txt, compare-export-before.txt, deleted-endpoint-before.txt |
| FIX-08 | 8b0f11c | tests.test_recommender_prediction_acquisition; final-suite.txt, remediation-market-models.md |
| FIX-09 | a59fa6a | tests.test_recommender_advisor_workflow, tests.test_recommender_advisor_execution, tests.test_recommender_child_planning; final-suite.txt |
| FIX-10 | bc93898 | tests.test_recommender_api_authorization, tests.test_recommender_postgres_store; final-suite.txt, remediation-control-web-results.txt |
| FIX-11 | 5931a50, 8fbabc4 | tests.test_recommender_browser_e2e; final-suite.txt, final-browser-suite.txt, reprice-browser-* |
| FIX-12 | ebfe498; final documentation/evidence commit | tests.test_recommender_cloud_acceptance + full discover/browser; cloud-entry-before.txt, cloud-cleanup-after.txt, final-suite.txt |

Result filenames in this table live under [evidence/](evidence/), except the named
companion Markdown documents and remediation-control-web-results.txt.

## Final local validation

Environment: macOS, Python 3.12, this checkout's locked uv environment with all
declared dependency groups, Playwright 1.58.0 and real headless Chromium v1208.

```bash
uv sync --locked --all-groups
ATSUITE_BROWSER_E2E=1 uv run --locked --all-groups python -m unittest discover -s tests -p 'test_*.py' -v
git diff --check
```

**241 tests run in 132.804 seconds: 240 passed, 1 skipped**. The single skip is
the explicitly unconfigured live PostgreSQL service. All three real browser E2E
tests passed in this combined run; browser/default execution used only explicitly
simulated external APIs. Full output: [evidence/final-suite.txt](evidence/final-suite.txt).
No paid cloud, real LLM or production apply action ran. This completes local
implementation and verification; full external acceptance remains open.

An earlier final run failed the unchanged two-version browser assertion after
repricing. [evidence/reprice-browser-before.txt](evidence/reprice-browser-before.txt)
preserves that failure. The UI now renders the returned immutable version
directly, selects it, and prevents older report reads from overwriting it. It
does not add redundant report fetches. The successful combined run above retained
the original five-second assertion and all 600 confirmation attempts.

Additional targeted command after strengthening the browser assertions:

```bash
ATSUITE_BROWSER_E2E=1 uv run --locked --all-groups python -m unittest tests.test_recommender_browser_e2e.BrowserWorkflowTests.test_upload_plan_approval_queue_polling_worker_evidence_and_exports -v
```

**1 passed in 108.792 seconds**, including automatic selection of the returned
reprice version and equality of the original report digest. Output:
[evidence/reprice-browser-after.txt](evidence/reprice-browser-after.txt).
