# FIX-06, FIX-08, FIX-09 validation record

Read repository AGENTS.md and full remediation plan. At reviewed HEAD
`de752f21342162dd4412ea9dd3b6e224a1f633fd`, before production edits,
project `.venv/bin/python -m unittest discover -s tests -p '<file>' -v`
reproduced five accounting failures, two feature failures, and two advisor failures:

- market_accounting: candidate aggregation, per-attempt rounding, duplicate region
  overwrite, missing amount represented as zero, monthly concurrency multiplier.
- prediction_acquisition: mapping candidates lose resources; providers indistinguishable.
- advisor_workflow: seconds interpreted as milliseconds; nested secrets retained.

Root captured the locked original baseline: 93 tests passed. This workstream did
not switch/reset/rebase/stash/commit/push.

Implemented complete Decimal selectors including unit compatibility, per-event
rounding, shared account-period tiers, actual meter-ID deduplication, candidate
attribution, undefined missing amounts, explicit unit conversion, safe dated ECB
FX, immutable snapshots/ledgers, stale checks at use, six built-in service catalog
selectors, raw observed provider metrics and ledger adaptation. Formal runtime
cost is separate from experiment spend. Cost uncertainty resamples actual
workflow block costs with the correct-success denominator; failed and unbounded
samples are retained. Reprice rebuilds candidate costs/intervals through the
canonical service without overwriting the parent report or losing simulation
labels. Actual timestamps label usage envelopes and billing periods; they never
replace measured billing duration.

Prediction uses versioned full candidate identities/resources, scaled validated
ridge targets, isolated family/time holdouts and duplicate-trial checks. Quality
evaluation records acquisition error/recall/regret and real trial calls/spend.
Default exploration derives numeric provider/family summaries from fresh pilots
and actually dispatches model-guided
next picks, preserving provider/family coverage and exploration. Missing,
unqualified, failing or OOD models execute grid fallback; predicted values never
become confirmation evidence. Selection audits bind the training-data digest and
pilot inputs. Model registry versions are immutable and reject path escapes.

Advisor intent produces typed constraints, seconds-to-ms conversion, explicit
hard-limit preservation and unresolved ambiguity. New proposals pass the signed
search boundary plus the exact ordinary topology/state/capability compiler,
become immutable children, and consume ordinary build/deploy/trial quotas. Frozen
parent source and base images are retained. Valid-model and no-model grid children
actually enter build/publication/deployment/pilot/confirmation/cleanup. Illegal
proposals cannot execute. A failure-first child deployment resume regression
proved the need to resume ordinary stages before dispatching the durable child.
Recursive whitelists redact catalog and prompt secrets. Claims are checked for
candidate/value/unit/scope/currency; model prose cannot replace the winner.
Pre-request token reservation, configured model/output/timeout and audited
refusal/parse/evidence/API template fallbacks are implemented.

After dependency synchronization, the focused command was:

```bash
UV_CACHE_DIR=/private/tmp/atsuite-uv-cache uv run --no-sync python -m unittest \
  tests.test_recommender_market_accounting \
  tests.test_recommender_prediction_acquisition \
  tests.test_recommender_advisor_workflow \
  tests.test_recommender_advisor_execution \
  tests.test_recommender_default_execution
```

The initial focused run passed 54 tests. Subsequently added failing unit-selector,
hourly-FC, numeric-pilot and immutable-model-registry regressions brought accounting
to 30 and prediction to 9 tests. The latest combined run including existing
platform tests passed 83 tests. An ordinary escalated `uv run` accounting command
also passed. Earlier sandbox cache/network failures are not test passes. Root
records full-suite and browser E2E verification. External transport fixtures are
explicitly simulated; no simulated outcome yields a cloud-verified winner.

Real cloud acceptance: **blocked_external**. No real cloud experiment, registry
publication, provider invoice reconciliation or paid model request was performed
by this workstream. Real model quality: **insufficient_real_data** until isolated
quality-checked real holdout observations exist. Online runtime catalogs alone
do not establish ancillary build/registry/logging/storage/cleanup bounds: approved
frozen bounds are required and unknown values stop paid dispatch. Unavailable
actual resource allocation or required idle/storage/network meters remain
explicit coverage gaps. The frozen Alibaba FC rule now rounds CU to an integer
per function/hour before account-region monthly tiers, and attributes the
USD0.01 active-function/hour minimum proportionally to billable quantity across
trials/phases. Missing resource/hour identity or unsplit spanning-hour intervals
cannot be priced. Conservative dispatch bounds include hourly rounding/minima.
The policy source is the [official FC billing documentation](https://www.alibabacloud.com/help/en/functioncompute/pay-as-you-go-billing-methods),
updated August 25, 2026; it is frozen with the snapshot and does not substitute a
static CU unit price for a live catalog. This is local policy implementation,
not verified external invoice reconciliation.
