# Recommender implementation status

Status date: **September 16, 2026**
Branch: `feat/recommender`
Baseline: `main@43dd80b802050e5138648fce9f00a348767ad5fe`

The labels below are intentionally separate:

- **Implemented**: production code path exists.
- **Unit tested**: deterministic local tests cover the core branch and failure state.
- **Integration tested**: multiple local components were exercised together.
- **Cloud verified**: a real provider account completed build, deploy, replay, readback, price coverage, and cleanup.
- **Model evaluated**: an unseen workload evaluation report meets an agreed quality gate.

| Package | Scope | Implemented | Unit tested | Integration tested | Cloud verified | Model evaluated |
|---|---|---:|---:|---:|---:|---:|
| R01 | replay timing, tool failure, output capture/oracle hooks, strict metric availability | yes | yes | replay output/correctness sidecars | unverified | n/a |
| R02 | contracts, capabilities, fractional CPU, state identity | yes | yes | local runtime | unverified | n/a |
| R03 | recursive build identity, isolation, digest handoff, worker pool | yes | yes | build orchestration with fake builder | unverified | n/a |
| R04 | six topology strategies, compile, validation, stable IDs | yes | yes | generated config resolution | unverified | n/a |
| R05 | AWS/GCP/Alibaba pricing, ECB/OXR FX, Decimal billing, immutable repricing | yes | yes | fixture parsers and usage-ledger replay | blocked_external | n/a |
| R06 | strict evidence, constraints, Pareto, fixed anchors, inconclusive states | yes | yes | decision fixtures | unverified | n/a |
| R07 | stage machine, budget primitives, exploration/confirmation selectors, resume/cancel/cleanup | yes | yes | persistent job driver with cleanup failure recovery | unverified | n/a |
| R08 | dataset, grouped/time split, training, registry, prediction, OOD fallback | yes | yes | local deterministic dataset | n/a | insufficient_real_data |
| R09 | intent, Responses Structured Outputs, redaction, verifier, fallback/audit | yes | yes | fake Responses client | blocked_external | n/a |
| R10 | FastAPI, SQLite jobs/events, idempotency, tenant ownership, SSE, safe upload, artifact ACL | yes | yes | FastAPI TestClient | unverified | n/a |
| R11 | six Web views, status labels, progress/actions, export, keyboard/reduced-motion basics | yes | static checks | API served locally | browser_e2e_not_run | n/a |
| R12 | docs and test matrix | partial | yes | local suite | blocked_external | insufficient_real_data |

## Automated evidence

Local standard-library suite:

```bash
UV_CACHE_DIR=/tmp/atsuite-uv-cache uv run --no-project --offline \
  python -m unittest discover -s tests -p 'test_*.py'
```

Project environment integration checks:

```bash
.venv/bin/python -m unittest \
  tests.test_recommender_platform.OptionalIntegrationTests -v
```

Coverage added for strict contracts, fractional CPU, topology constraints, build reuse, recursive source hashing, digest references, partial/complete resource readback mismatch, business output capture, correctness sidecars, cancellation cleanup and resume, market pagination and tiers, immutable repricing, FX conversion, billing deduplication, fixed-anchor decisions, predictor OOD, state isolation, ZIP traversal/symlink rejection, idempotency conflicts, tenant artifact ownership, FastAPI routes, and Advisor redaction.

## External verification still required

No paid cloud operation or real model API call was executed during this implementation session.

Before a production release, record at least one supported region/account profile per provider with:

1. capability and effective configuration readback;
2. registry digest publication and digest deployment;
3. build, deploy, replay, metric provenance, billing coverage, and cleanup;
4. failure and cancellation cleanup evidence;
5. provider price SKU coverage;
6. an unseen workload predictor report with MAE, acquisition recall, regret, and spend;
7. browser automation for upload → plan → progress → comparison → evidence → export → cancel/resume → reprice;
8. a real Advisor call with token usage and deterministic verification.

Until those records exist, provider rows remain `blocked_external` or `unverified`; model quality remains `insufficient_real_data`.
