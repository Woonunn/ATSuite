# Wabber request routing prototype

`OnlineRouter` joins the existing WorkloadModel and ProviderModel. It predicts
before arrival, counts all completed attempts in empirical SLO risk, and admits
one whole Session into one provider/mode. It does not modify Tool state handling:
MCP retains state; FaaS restores/persists at each Tool call.

This is a process-local research prototype, not a production SLO guarantee.

## Decision contract

Construct `OnlineRouter(workload, provider, pools, limits, log_path=...)`, where
`pools` maps stable execution-pool names to ProviderModel pool configurations.
`limits` contains independently validated **Session admission limits**. These
are not provider invocation concurrency or inferred instance counts. Use `None`
when unknown. One router must exclusively own admission into these pools;
external occupancy must be deducted from the limits before starting the run.

`decide(request_id, task, load_rps, as_of_ns=..., shadow=False)` returns a frozen
explanation and reserves capacity atomically. Repeated IDs return the original
decision. `selected=None` means no trusted feasible target, not a hidden default.
A rejected request must not be dispatched by the caller. For differing pool loads,
supply `pool_load_rps={pool_key: observed_rate, ...}` (also supported by the
LangGraph routing context); predictions and completion feedback use each pool
rate. Without it, every candidate is evaluated at the offered rate, explicitly
logged as `offered_load_per_candidate`; no mixed routing load reduction is assumed.

Risk uses each distinct attempt once, with only the latest evidence revision
available strictly before arrival. Failed/incomplete attempts and missing latency
count as violations. A failure at any latency counts; success exceeds the SLO
only when latency is strictly greater than the threshold. Wilson's two-sided 95%
upper endpoint is compared with the configured risk ceiling. Defaults: 120 s,
5%, at least 60 observations. Repeated attempts are not independent-task evidence;
the interval assumes exchangeability and is not distribution-free.

Calibration matches task, exact pool/resources/instance configuration and a
narrow historical load envelope (query load to 1.25 times query load). No unseen
task/load is silently treated as safe. Missing predictive features are retained
in ProviderModel coverage; a validated empirical task/load envelope can provide
a fallback when the latency model is unreliable. This deliberately conservative
method does not extrapolate to changed deployments or cold-start regimes.

Among candidates that pass capacity and risk gates, reliable-model candidates
are preferred. Comparable complete per-request cost forecasts rank them by cost;
otherwise empirical upper risk ranks them. Currency and scope must agree across
all candidates. Estimates remain estimates, including invoice-trained forecasts.
Ranking applies only to the named complete scope; it does not establish total-bill
savings. Partial costs and different currencies cannot establish cost savings.

`complete(request_id, trace)` updates both models and releases admission. The
trace must retain the admitted request ID and arrival time, including time spent
routing. Failures must be completed too. Shadow decisions do not reserve slots;
complete them with `actual_pool=...` to update only the actual executing pool.
JSONL logs retain inputs, predictions, coverage, risk, reason and outcomes. Model
snapshots use existing `.save()` methods. Active sessions are not restartable.

## LangGraph integration

`experiments.exp1.capture.capture_agent_traces.run` accepts `router` and
`routing_context` keyword arguments. Existing callers remain unchanged.

```python
await run(
    task_id,
    base_config,
    output,
    capture=capture_path,
    router=router,
    routing_context={
        "load_rps": current_load,
        "targets": {
            pool_key: {
                "tool": audited_tool_config,
                "capture": environment_reference_path,
            },
        },
    },
)
```

Supply every pool in `targets`. Each Tool configuration follows the existing
Capture/Replay contract. A target's capture must be the task-specific sealed
reference for that environment. In shadow mode add `shadow=True` and
`actual_pool=pool_key`; the base Tool configuration must match that actual target.
The hook records decisions before opening ToolClient and handles success/failure
feedback in the existing `finally` path. It does not add retries or migrations.

This hook does not replace deployment verification, Pilot, budget admission or
arrival scheduling. Paid mixed-run orchestration is not yet supplied. Reuse the
existing sealed workload, `run_open_loop`, deployment/reference verification,
Pilot correctness checks and cumulative budget ledger before any cloud dispatch.
Do not reuse a static-provider admission as evidence for an unvalidated mixed run.

## Reproduce offline validation

```sh
uv run python -m atsuite.routing.evaluate \
  --e1a results/phase4/e1a-formal-useast-seed11-v1 \
  --output results/router/e1a-new
uv run python -m atsuite.routing.evaluate \
  --e1b results/phase5/cloud-fixed-typical-envref-v3/formal \
  --output results/router/e1b-new
uv run python -m unittest tests.test_routing tests.test_provider_model \
  tests.test_workload tests.test_e1b_cloud -q
```

Output directories must be new. The CLI writes source hashes, causal predictions,
60/40 temporal evaluation and six task holdouts for E1-B. Task-holdout runs never
train on the held task. Inputs use the latest 20 completed successful profiles;
failed attempts still calibrate risk. Frozen comparisons freeze ProviderModel,
not the incoming workload history. Loads are sealed-window planned rates in
these replay artifacts, not future-informed estimates for live traffic.

Reports separate latency error, p95 attainment, SLO violations, success, Brier
score, reliability bins, load/task strata and measurement coverage. Missing
provider queue/cold-start data remain unknown. E1-B imports application cold-start
flags and error details separately; process-first-task flags are not provider
cold starts. E1-B currently has no delayed vendor enrichment or complete bills. E1-A identical
frozen profiles are represented once rather than repeating the same query 20 times.

A sampled historical shadow pass (every 31st offered arrival, selected without
looking at outcomes) is reproducible with:

```sh
uv run python -m atsuite.routing.shadow \
  --e1b results/phase5/cloud-fixed-typical-envref-v3/formal \
  --output results/router/shadow-new --rho-threshold 1000
```

The threshold is a declared baseline parameter, not fitted to the test results.
All observations may be preloaded, but availability filters remain strict at each
historical arrival. Capacity stays unknown rather than inventing a mixed-run
occupancy. This is offline shadow replay, not a live cloud shadow campaign.

`shadow.baseline_choices` implements fixed pool, round robin, lowest predicted
mean and a rho rule with an explicit threshold. `shadow.decision_quality` compares
frozen decisions with an explicitly matched complete provider outcome matrix.
Its output is a ranking diagnostic, never mixed-routing latency/cost. Dynamic
reassignment changes queues, concurrency and cold starts. Real mixed-routing p95,
SLO, success and total-cost gains remain unverified until controlled cloud runs.

## Current evidence and remaining acceptance work

The imported latest E1-B run has 764 attempts (191 per provider), all successful,
with only 31–33 observations per task/provider. All 31 `human_7` attempts in each
pool exceed 120 s; success does not imply SLO attainment. Other tasks have zero
observed violations but Wilson upper bounds of 10.4%–11.0%. This cannot establish the default
5% bound with 60 required samples. Physical pool capacities and complete costs
are also unknown. Refusal to admit from this evidence is expected.

Completed: routing core, LangGraph live/shadow hooks, empirical calibration,
E1-B ingestion, causal temporal/task evaluation, baseline/ranking utilities and
local regression coverage. Not completed: a budget-admitted mixed-cloud driver,
real shadow campaign, controlled mixed routing comparisons, and actual-bill cost
optimization. No paid requests or deployments were made for this implementation.
