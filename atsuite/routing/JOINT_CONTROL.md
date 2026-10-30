# Stage 5: joint online routing and capacity control

This implementation is a process-local experimental controller. It adds real SDK
capacity operations and a mixed-cloud Replay entry point. **A controlled cloud
joint experiment and a measured cost saving have not been established.** Existing
E1-A/E1-B observations do not cover the new capacity regimes or supply the
independent service/cold/queue measurements needed to validate the simulator.

## Capacity semantics, verified 2026-10-29

| Existing deployment | Control implemented | Effective readback | What remains unknown |
|---|---|---|---|
| AWS Lambda, unqualified endpoint | `PutFunctionConcurrency` | `GetFunctionConcurrency`, function readiness | physical instances, warm capacity, independently measured Session limit |
| AWS Lambda, separately audited version/alias | provisioned concurrency put/delete | requested / allocated / available / status | total on-demand instances; the current E1-B driver does not invoke an alias |
| AWS AgentCore microVM | managed, read-only | `GetAgentRuntime`, readiness/lifecycle/version | physical instances and Session quota; no fabricated instance target |
| Alibaba FC | `PutScalingConfig(min_instances=...)` | min / current, readiness and concurrency quota | total on-demand instances and Session admission capacity |
| Alibaba FC Session MCP (`ali_agentrun`) | same FC API, with whole-pool drain before reduction | same readback, frozen affinity/lifecycle | platform Session release and unexposed per-instance drain state |

The four repository backends are Lambda, AgentCore microVM, FC FaaS, and FC
Session MCP. `ali_agentrun` is not a separate AgentRun API deployment. AgentCore's
new Instances compute type is not the deployed microVM architecture. Selecting it
would change the frozen experiment and is rejected by this adapter.

Current read-only queries succeeded for all four deployments: Lambda quota 64;
FC minimum 0 and request quota 64 for both modes; AgentCore READY and managed.
The readback contains no credentials or environment values. No cloud resources
were changed and no paid Replay requests were issued during this implementation.

Official API references:

- [Lambda reserved concurrency](https://docs.aws.amazon.com/lambda/latest/api/API_PutFunctionConcurrency.html)
- [Lambda provisioned concurrency readback](https://docs.aws.amazon.com/lambda/latest/api/API_GetProvisionedConcurrencyConfig.html)
- [FC scaling readback](https://www.alibabacloud.com/help/en/functioncompute/api-fc-2023-03-30-getscalingconfig)
- [FC scaling update](https://www.alibabacloud.com/help/en/functioncompute/api-fc-2023-03-30-putscalingconfig)
- [FC Session isolation and release](https://www.alibabacloud.com/help/en/functioncompute/session-isolation-configuration)
- [AgentCore compute types](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-how-it-works.html)

`ScalingAdapter.from_deployment` reuses the existing AWSLambda/AWSAgentCore/AliFC
objects and their SDK clients. It does not redeploy images, change affinity,
shorten Session lifetimes, or alter state handling. MCP keeps its resident state;
FaaS keeps the existing restore/persist-per-Tool behavior.

## Controller contract

`JointController` wraps `OnlineRouter` and can be passed into the existing
LangGraph `router=` hook. `tick(task_rates, queued=..., utilization=...)` is the
control step; `decide` and `complete` retain whole-Session binding. The live
runner calls `tick` independently of arrivals, including during idle periods.
Defaults are a 10 s period, 60 s forecast horizon, 60 s cooldown, 30 s trailing
arrival window and 70% target Session utilization. All are explicit run inputs.

Each pool has a finite set of **calibrated** levels. A level contains:

- `target`: SDK control value; `null` only for a managed runtime.
- `pool`: the ProviderModel pool identity, with `instances: null` and
  `resources.capacity_control = {control, target}`. Physical instance counts
  cannot be inferred from the configured quota or warm floor.
- `session_limit`: measured whole-Session admission limit, independent of RPC
  quota, per-instance request concurrency and physical instance count.
- `scale_delay_s`: a measured conservative scaling-delay estimate.
- `evidence`: calibration references. Cloud runs additionally seal evidence
  files by SHA-256 before applying any changes.
- `cost`: complete, disjoint cost rates described below.
- `tool_slots`: independently validated concurrent Tool slots, used by simulation.

A target is never a ready capacity observation. Capacity reads publish limits
atomically; failed reads, readiness transitions and unexpected deployment drift
close admission. The adapter queries function resources as well as control
configuration. A successful put does not release pending capacity: a later read
must confirm it. Ambiguous API errors remain pending for reconciliation and are
not blindly retried. The prototype has no automatic crash recovery.

A scale-in stops new admission and waits until **all** locally owned Sessions
finish. It does not pick an arbitrary instance based on a pool-wide active count.
The existing MCP client closes HTTP connections without acknowledging remote
Session deletion. Completed MCP requests therefore retain their capacity
reservation for the configured **maximum Session lifetime after completion**.
That deliberately conservative bound comes from the frozen lifecycle settings;
it is not a measurement of physical destruction time. Missing lifecycle settings
reject a state-coupled level. The tail contributes to demand, cost and simulation
billing; cloud restoration waits for it before reducing capacity.
Unconfirmed ToolClient cleanup holds the reservation and prevents scale-in. An
operator must reconcile a failed cleanup before restarting control. Exclusive
ownership of the pools is required; untracked external Sessions are unsupported.

Capacity changes mark active requests as transitions. Completion uses the
admission-time pool snapshot; transition outcomes remain logged but are excluded
from stationary ProviderModel prediction and empirical risk calibration. Their
transition marker is immutable across later evidence revisions.

## Joint search and baselines

`candidate_plans` enumerates the Cartesian product of supplied levels and routing
weight vectors, capped at 256 candidates (larger grids fail explicitly). Each
plan specifies both all capacity targets and all routing shares. Weights are
whole-Session weighted round-robin shares, not per-Tool migration instructions.

For every plan, the controller:

1. Predicts using the existing joint Workload profiles and ProviderModel. For each
   offered task and nonzero pool share it evaluates Stage 4's exact task/config/
   load empirical risk, including failed attempts. The Wilson upper bound and
   minimum sample gates are unchanged; low-confidence predictions retain the
   existing empirical fallback.
2. Estimates Session demand from per-pool load and predicted Session p95, which
   includes LLM waits, Tool/state work and queueing. It adds queued Sessions and
   checks the measured Session limit, cooldown and scaling-delay horizon.
3. Forecasts cost for **every pool**, including pools with zero routing share,
   existing retained Sessions, storage and scaling overhead. Expansion includes
   old-floor startup overlap. A busy pool with unknown drain time retains its old
   floor for the entire horizon. Storage is not added again during floor overlap.
4. Chooses the lowest complete, comparable predicted total among feasible plans.
   No feasible plan produces `no_trusted_feasible_plan`; missing cost cannot be
   interpreted as a free pool. This is an estimate, not an SLO or billing guarantee.

Dispatch checks actual readback again and applies Stage 4 risk gates at the larger
of observed pool traffic and planned traffic. It rejects instead of rerouting
unaccounted load into another pool. A policy older than two periods is unusable.
Expected expansion does not waive current risk or admission checks.

Comparison policies use the same risk gates and observations:

- `static`: initial capacity and first declared routing split.
- `routing`: frozen initial capacity, candidate routing shares ranked by complete
  predicted cost, or risk when costs are unavailable.
- `independent`: first routing split, smallest demand-feasible calibrated capacity;
  supplied high-utilization observations trigger expansion, with common cooldowns.
- `joint`: joint target/share search described above.

This is a deliberately small finite search. It does not forecast future tasks
from future arrivals, learn a routing policy, or claim exhaustive global optimality.
A sustained controller failure stops useful admission; it never creates a hidden
unsafe fallback. Queue rejections remain in offered-load and outcome denominators.

## Cost contract

`cost.forecast_cost` requires `scope: total_infrastructure`, `complete: true`,
`evidence`, a provider currency, and
`exclusive_components: [execution, retention, scaling, storage]` (sorted).
Rates are explicit, including zero rates:

| Field | Scope |
|---|---|
| `per_request[task]` | request execution only; excludes all shared/retained/storage charges |
| `retention_per_s` | the entire configured pool floor, busy and idle together |
| `session_per_s` | additional retained Session occupancy outside that floor only |
| `storage_per_s` | shared state storage and storage overhead for this pool |
| `per_change` | cost of one capacity change |

These rates need measurement/pricing evidence for each level. They are not
hardcoded vendor prices and cannot be filled from incomplete invocation subtotals.
The controller conservatively charges existing Sessions for the full horizon.
The frozen FX input records `currency`, `rates` (native to reporting currency),
`date`, and `source`; the base-currency rate must be 1. Decimal arithmetic is used
for financial sums before JSON serialization.

`total_cost` reconciles exclusive charge lines by `(provider, charge_id)`, rejects
conflicting duplicates and overlapping resource/component intervals, and keeps
predicted, estimated, actual and mixed bases explicit. Each resource must cover
execution, retention, storage and scaling, including explicit zero lines. Partial
or unequal interval coverage yields a subtotal and `total: null`. The reported
scope is explicitly limited to the supplied resources. Inclusive bills must replace their
already-included request lines, not be appended to them. Invoice collection and
attribution continue to use the existing billing/Analyzer evidence machinery;
this implementation does not manufacture missing invoices.

## Offline simulation

`load_sealed_workload` validates the existing E1-B seal, imports original Copilot
arrival offsets and task assignments, and reads Capture ATG dependencies. It keeps
recorded LLM waits but discards **all historical Tool durations**. Select one
scenario/window; overlapping windows cannot be merged into a new traffic trace.
Keep the original trailing idle interval, and declare any additional drain time.

`ServiceSurface` requires independent training and validation JSON arrays. Rows:

```json
{
  "id": "unique-tool-measurement", "request_id": "attempt-id",
  "source_run": "independent-run-id", "basis": "measured",
  "pool": {"provider": "aws_lambda", "environment": "sealed-environment",
           "resources": {"memory_mb": 4096,
             "capacity_control": {"control": "reserved_concurrency", "target": 64}},
           "instances": null, "sessions_per_instance": null,
           "invocations_per_instance": 1},
  "task_id": "human_15", "tool": "2:prepare_workspace",
  "load_rps": 0.5, "concurrent_sessions": 2,
  "service_s": 0.1, "cold_start_s": 0.02,
  "queue_excluded": true, "status": "success", "evidence": ["measurement-path"]
}
```

The numbers above illustrate the schema; they are **not measured evidence**.
For imported ATGs, `tool` is `node_id:node_name`, preventing different code steps
with the same Tool name from being pooled together. Service time includes Tool
execution and state synchronization, excludes queue time, and separates measured
cold-start delay. Unknown components block fitting. Request IDs, sample IDs and
source runs must be disjoint across training/validation. Exact configuration and
Session-concurrency strata need at least ten samples in each split and a default
held-out p95 relative error of at most 30%. This is a model diagnostic, not proof
of tail-risk coverage or independence of concurrent samples.

The simulator queues Tool invocations under `tool_slots`, holds a Session through
its entire DAG and LLM waits, samples service/cold/failure outcomes at the current
pool load and concurrency, and simulates delayed capacity readback. Missing
validated strata mark the run invalid. The fixed seed, inputs and hashes are
recorded. End-of-window active Sessions are reported incomplete. Simulation
results carry `basis: simulation`, `actual: null`, and a synthetic marker if any
service inputs are not explicitly measured.

Reports include all offered/admitted/successful requests, successful-request p95,
admitted SLO violations, offered unsatisfied rate (including rejections), Session
utilization, scaling events and disjoint estimated cost. JSONL control logs contain
capacity observations, requested/effective changes, drain/error events and every
candidate's costs, risk and reason. Readback times bound observed responsiveness;
they are not exact platform instance creation/destruction timestamps. Vendor
per-instance lifecycle telemetry remains unknown where the API does not expose it.

## Reproduction

Run from the repository root; output directories must be new.

```sh
# Current deployment/configuration reads only; no mutations or Tool invocations.
uv run python -m atsuite.routing.experiment \
  --readback-deployment results/phase5/cloud-fixed-typical-envref-v3/deployment.json \
  --output results/joint-control/readback-new

# Audit actual evidence, with a risk-coverage figure. E1-A is also accepted via --e1a.
uv run python -m atsuite.routing.experiment \
  --e1b results/phase5/cloud-fixed-typical-envref-v3/formal \
  --output results/joint-control/evidence-new

# 191 original arrivals and six sealed task DAGs; no fabricated Tool timings.
uv run python -m atsuite.routing.experiment \
  --sealed-workload results/phase5/workload-fixed-typical-v1 \
  --output results/joint-control/inputs-new

# Reproducible synthetic CLI smoke test. Its four identical one-pool results
# check integration only; they demonstrate no optimization benefit.
uv run python -c 'from tests.test_joint_control import write_simulation_fixture; write_simulation_fixture("results/joint-control/fixture-new")'
uv run python -m atsuite.routing.experiment \
  --simulation results/joint-control/fixture-new/config.json \
  --output results/joint-control/comparison-new

uv run python -m unittest tests.test_joint_control tests.test_routing \
  tests.test_provider_model tests.test_workload tests.test_e1b_cloud tests.test_phase3 -q
```

The generated `config.json` is a runnable configuration example. Replace its
synthetic model snapshots, cost rates, capacity evidence and service datasets with
measured inputs. For real traffic use `sealed_workload` instead of `arrivals` and
`workflows`. The CLI runs all four strategies from separate model snapshots and
writes JSON, JSONL, PNG and PDF outputs. Explicit seed/start/deadline values are
required; `fx` is a frozen run input, not a live conversion.

## Cloud entry point and remaining experiment work

`experiments.exp1.runner.run_joint` uses the existing deployment validation,
per-task four-backend Pilot checks, environment-reference seals, open-loop
scheduler and cumulative `reserve_budget` ledger. The mixed driver additionally
requires a non-synthetic independently validated joint simulation that exercised
scaling, admitted requests, and met the offered unsatisfied-rate ceiling.
Evidence files, including simulation and mixed-Pilot reports, must be hash-sealed.
Simulation admission also verifies the same control parameters, model contents,
source-code hashes and sealed workload; changing any of these requires new evidence.
A formal run requires a successful matching mixed Pilot with at least one observed
effective capacity change. It never reuses a static Pilot as mixed-run evidence.

The run configuration extends the simulation/control example with:

- All four audited pools, complete measured capacity levels and initial targets.
  Copy the deployment's actual resources, update only the controlled field
  (`reserved_concurrency` or `min_instances`), and include `capacity_control`.
- `initial_task_rates`, `harness_concurrency`, `tool_rpc_timeout_s`.
- `simulation_report`, `evidence: [{path, sha256}]`; for a formal run also
  `joint_pilot_report`. Paths in evidence are relative to the repository root.
- The existing `budget` contract (`aws_usd_admission_cap`, `ali_cny`,
  `reserve_fraction`) and `budget_reservations.aws/ali = {amount, basis}`.
  Bounds must cover the entire run, warm/idle tails, ancillary charges and
  restoration. Existing reservations remain held; these are not invoices.

```sh
uv run python -m experiments.exp1.runner.run_joint \
  --config measured-joint-config.json \
  --workload results/phase5/workload-fixed-typical-v1 \
  --deployment results/phase5/cloud-fixed-typical-envref-v3/deployment.json \
  --pilots results/phase5/cloud-fixed-typical-envref-v3/pilot \
  --references results/phase5/cloud-fixed-typical-envref-v3/references.json \
  --output results/joint-control/cloud-preflight-new --pilot --preflight-only
```

After successful preflight, run the Pilot without `--preflight-only`, then supply
its sealed report for a formal run. `--policy` selects static/routing/independent/
joint; keep the workload and experimental settings fixed across comparisons.
The driver reserves budget before cloud writes and dispatch, runs control during
Replay, and restores initial targets only after local Session drain. Restoration
is polled for up to 120 s; unresolved cleanup marks the run incomplete and reports
retained capacity instead of terminating Sessions. Each request records actual
route, errors, latency and SLO outcome; no invoice-supported total is invented.

**Outstanding acceptance work:** collect per-level capacity/transition and
Tool/cold/queue measurements from independently separated runs; establish complete
shared-cost rates and budget bounds; validate simulation under those regimes;
execute and validate the mixed cloud Pilot/formal comparisons; reconcile vendor
billing and plot measured comparisons. Current E1-B preflight has 24 task/provider
strata and blocks all of them at the default 60-sample/5% risk setting. The
`human_7` attempts exceed the 120 s SLO; loosening risk gates just to admit them
would change the declared experiment. No claim of real latency or cost improvement
is supported by the implementation checks.
