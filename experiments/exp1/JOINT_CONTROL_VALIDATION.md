# Stage 5 implementation validation — 2026-10-29

Base: `main` at `cb0ac1b`. The working-tree change adds joint control; it does not
complete the requirement for a controlled real-cloud joint experiment.

## Completed checks

- Expanded regression: 91 tests, 89 passed, one timeout and one skip. This includes routing, provider and
  workload models, E1-B cloud guards, arrival scheduling, LangGraph, local MCP
  process isolation/residency and FaaS state synchronization. The skip is the
  existing numerical sandbox integration check (`sklearn`/`joblib` unavailable).
  The older residency test imports `phase0_environment` as a top-level module;
  its invocation requires `PYTHONPATH=tests:.`. The existing sixteen-process
  residency check returned one Tool timeout in the expanded run, then passed in
  an isolated rerun (11.5 s). Both logs are retained as `regression-final.log` and
  `residency-recheck.log` under `results/joint-control/`; the timeout was not hidden
  by changing its assertions or deadline.
- The 21 new joint-control tests were rerun after the final capacity/accounting
  corrections and passed. They cover target versus effective capacity, SDK calls,
  active-Session scale-in protection, cancelled drains, uncertain cleanup,
  configuration-transition feedback, stale policies, conservative cost/FX,
  completed MCP Session residency tails, simulation/controller identity binding,
  duplicate and overlapping bills, independent simulation validation, seeded
  Tool resampling, multi-pool routing/capacity decisions, queued SLO budget,
  sealed DAG conversion, and no cloud mutation when preflight is infeasible.
- Ruff checks pass on the routing package, touched provider-model modules,
  new cloud runner and new tests. `git diff --check` passes.
- The simulation CLI ran all four policies from separate snapshots. Its one-pool,
  four-request **synthetic smoke fixture** admitted and completed 4/4 requests
  for every policy, with identical 0.35 s p95. It demonstrates reproducibility
  and CLI integration, not optimization benefit. JSON, JSONL, PNG and PDF exist
  under `results/joint-control/synthetic-comparison-v1/`; the final source-hashed
  reproduction is `results/joint-control/synthetic-comparison-v2/`.
- Imported the actual fixed/typical sealed workload: 191 Copilot arrivals and six
  DataSciBench DAGs. Tool execution durations are absent from the imported timing
  model; LLM waits and DAG dependencies are retained. Artifacts are under
  `results/joint-control/sealed-fixed-typical-v1/`.

```sh
PYTHONPATH=tests:. uv run python -m unittest \
  tests.test_joint_control tests.test_routing tests.test_provider_model \
  tests.test_workload tests.test_e1b_cloud tests.test_phase3 \
  tests.test_langgraph_agent tests.test_mcp_residency tests.test_logical_state -q
```

## Real cloud reads, not scaling validation

Readback succeeded for the current four deployments and includes current
configuration, readiness and supported capacity fields. The evidence is
`results/joint-control/readback-v2/readback.json`:

| Backend | Observed configuration |
|---|---|
| Lambda | reserved concurrency 64; memory 4096 MiB; READY |
| AgentCore | managed microVM; runtime version 3; READY; physical capacity unknown |
| FC FaaS | min instances 0; request quota 64; instance concurrency 1; READY |
| FC Session MCP | min instances 0; request quota 64; instance concurrency 200; Session-exclusive; READY |

No cloud capacity writes, Tool requests, deployment changes or new budget
reservations were performed. SDK mutation paths have mock coverage, **not proof
of real expansion/scale-in behavior under load**. Vendor APIs do not expose all
physical-instance lifecycle events; missing measurements remain unknown.

## Acceptance remains blocked by evidence

`results/joint-control/e1b-preflight-v1/preflight.json` and `risk_coverage.png`
audit 24 task/provider strata from the existing formal E1-B run. Missing inputs:

- exact capacity-control identities with independently calibrated Session limits;
- service-slot, provider queue and cold-start measurements at candidate levels;
- independently separated training/validation runs for the new simulator strata;
- complete disjoint execution/retention/idle/storage/scaling cost evidence.

Current per-task observations also fail the default 60-sample / 5% Wilson upper
risk gate. The measured `human_7` requests exceed the 120 s SLO. No old outcomes
were relabelled as successful mixed-routing predictions and no risk gate was
relaxed to force an experiment through.

The real mixed Pilot, formal strategy comparisons, actual effective scaling under
load, complete attributed bills and measured cost/latency benefits remain undone.
The [control guide](../../atsuite/routing/JOINT_CONTROL.md) specifies the executable
entry points, input contracts, budget guard, cleanup behavior and evidence needed
to perform those steps. The synthetic smoke report is deliberately ineligible as
a live-run admission artifact.
