# Stage 4 routing prototype validation — 2026-10-29

Base: main `a1e41c8`. No deployment, paid request, resource change or cloud bill
query was performed. The implementation is described in
[`atsuite/routing/README.md`](../../atsuite/routing/README.md).

## What is implemented

- Request-level provider/mode choice with an atomic process-local Session lease.
- All-attempt empirical risk and Wilson upper bounds, strict evidence cutoffs,
  explicit unknown capacity/task/load, rejection and empirical fallback.
- Scoped comparable cost forecasts, without implicit currency conversion.
- Existing LangGraph Capture/Replay integration for live routing and shadow mode;
  Session affinity and existing MCP/FaaS state lifecycle remain unchanged.
- Completion feedback to both models; late evidence preserves task identity.
- Full E1-B attempt ingestion, temporal/task holdouts, sampled historical shadow,
  fixed/round-robin/mean-latency/rho baseline decision diagnostics.

No budget-admitted mixed-cloud driver or live shadow campaign has been completed.
The routing hook is an integration interface, not a replacement for those gates.

## Actual datasets and temporal checks

E1-A: `results/phase4/e1a-formal-useast-seed11-v1`, 2,888 attempts,
2,749 successes and 139 failures. E1-B:
`results/phase5/cloud-fixed-typical-envref-v3/formal`, 764 attempts,
191 successful attempts per provider, covering six tasks.
The old E1-B local validation-summary is not used as the cloud completion record.

Canonical offline outputs: `results/router/e1a-v2/`,
`results/router/e1b-v1/`, `results/router/shadow-v2/`.
Source paths and SHA-256 hashes are preserved in provenance files.
The earlier empty E1-A v1 run was interrupted during redundant fixed-profile
queries; shadow v1 is superseded because stride 32 aliases a four-pool round robin.
The final shadow uses stride 31, preserving original request sequence indices.

Temporal validation uses the first 60% of each provider's arrivals as training,
with all tied arrivals at the boundary placed in the held-out set. Predictions
precede completion updates at equal timestamps. E1-A has 296 held-out attempts
per pool; E1-B has 77. Latency errors below are conditional on successful observed
requests; failure/SLO metrics retain every attempt.

| Dataset | Pool | MAE updating (s) | MAE frozen (s) | Predicted p95 attainment |
|---|---|---:|---:|---:|
| E1-A | AWS Lambda | 0.391 | 0.404 | 82.8% |
| E1-A | AWS AgentCore | 5.398 | 5.313 | 84.1% |
| E1-A | Aliyun FC | 0.451 | 0.423 | 82.8% |
| E1-A | Aliyun FC Session MCP | 0.607 | 0.609 | 91.1% |
| E1-B | AWS Lambda | 40.686 | 40.735 | 90.9% |
| E1-B | AWS AgentCore | 36.515 | 36.229 | 89.6% |
| E1-B | Aliyun FC | 29.164 | 29.283 | 97.4% |
| E1-B | Aliyun FC Session MCP | 25.996 | 25.992 | 92.2% |

Updates do not consistently improve error. E1-A's Session MCP held-out set has
94 failures excluded from latency scoring but included in risk evaluation.
A good p95 coverage number on a mixed task distribution does not demonstrate
accurate task-specific latency prediction or safe decisions on unseen tasks.

## Risk and decision findings

At the predeclared 120-second SLO, every `human_7` attempt violates the SLO:
31/31 in each pool, despite successful computation. Every other E1-B task has
zero observed SLO violations, but only 31–33 samples per pool. Its Wilson upper
risk is 10.4%–11.0%, above the default 5% ceiling. More repetitions alone would
not establish generalization to unseen tasks or load regimes.

All six task-holdout runs completed. They exclude the held task from every model
update, including calibration; `results/router/acceptance-checks.json` records
asserted holdout counts (124–132 per task across the four pools). Unknown held-task risk must remain unknown rather than zero.
Reports include per-task/load/provider status counts, calibration bins, Brier
scores, coverage and frozen-model comparisons. Historical loads are sealed
planned window rates; they are not live estimates derived from future arrivals.

The final sampled shadow pass scores 25 historical arrivals. The risk router
abstains on all 25 because capacity is unknown and risk evidence is insufficient.
Round robin selects each of four pools 6–7 times. Other baseline choices and
matched independent-pool regret are in `shadow-v2/diagnostics.json`.
These comparisons describe reference ranking only. They do not measure mixed
queues, cold-start changes, mixed end-to-end p95 or cloud cost savings.

## Checks and remaining acceptance work

The related suites cover 39 tests: router, ProviderModel, WorkloadModel and E1-B
cloud contracts. New checks cover timestamp ties, unseen task/load, capacity
exhaustion, concurrent admission, stable bindings, shadow target preservation,
failed completion feedback, late evidence identity, per-pool load snapshots, scoped cost choice and
future-outcome invariance. New modules pass Ruff and the diff has no whitespace
errors. Local mocks are not claimed as real cloud integration evidence.

Remaining before Stage 4 can be accepted in full:

1. Validate fixed pool Session capacity and its provenance, including external
   occupancy, cold-start and session-release conditions.
2. Gather independently validated calibration evidence for feasible tasks/loads;
   the current 120-second SLO is infeasible for the measured `human_7` workload.
3. Build and admit the mixed-cloud scheduler through deployment/reference checks,
   a successful Pilot and the existing cumulative budget ledger. Do not reuse
   static-provider admissions or silently relax risk thresholds.
4. Run a real shadow campaign and controlled mixed routing/baseline experiments
   on the same sealed arrivals, resources and task mix; preserve failures and
   measure routing overhead, queue delay, success, SLO and p95.
5. Reconcile complete attributable provider bills before claiming actual total
   cost optimization. E1-B queue/cold-start vendor enrichment is still missing;
   application first-task markers do not replace provider measurements.
