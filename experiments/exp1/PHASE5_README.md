# E1-B: Prompt composition sensitivity

This phase prepares and validates local experiments. Cloud experiments remain paused. The four candidate strategies are `aws_lambda`, `aws_agentcore`, `ali_fc`, and `ali_agentrun` (the existing FC Session-exclusive MCP adapter).

## Measured classes

`configs/e1b.yaml` fixes thresholds before observing provider outcomes. Labels may overlap:

- CPU-intensive: median CPU time >= 5 seconds and CPU time / wall time >= 0.8.
- Memory-intensive: maximum process RSS >= 768 MiB.
- State-intensive: maximum serialized reference state >= 64 MiB.
- Relatively lightweight: median wall time < 5 seconds, RSS < 768 MiB and reference state < 64 MiB.

This classifies `human_142` as CPU-intensive; `human_12` and `human_7` as CPU-, memory- and state-intensive; `human_15`, `csv_excel_34` and `csv_excel_29` as relatively lightweight. These are operational thresholds for the six measured candidates, not universal resource classes. Full measurements and environment are pinned from the Phase 1 task manifest. Phase 1 reference programs differ from captured Agent programs: reference pickle size is not measured Agent state-transfer cost. Replay records actual worker CPU, RSS and SDK synchronization separately, with missing observations left null.

## Paired scenarios

All scenarios use the same previously selected low/typical/high ten-minute Copilot windows, with 53/191/478 arrivals respectively. Each window has three fixed half-open stages: [0,200), [200,400), [400,600) seconds. Seed 11 fixes task allocation and provider order; captured computation parameters retain seed 0.

- `fixed`: equal task weights across all six tasks, unchanged throughout the window.
- `cpu_ramp`: `human_142` receives 10%, 50%, 80% stage weights; the remainder is divided equally among the other five tasks. Total CPU-class weight also increases, because two other tasks carry the CPU label.
- `memory_state_ramp`: `human_12` and `human_7` jointly receive 10%, 50%, 80%; the remainder is divided equally among the other four tasks.

Largest-remainder quotas followed by a seeded shuffle retain exact request counts and approximately match requested proportions despite integer rounding. Actual per-stage task counts are stored. Each scenario has 722 requests; the master table has 2,166 rows. Each of the four future providers must run the same table, yielding 8,664 executions for a complete matrix. No cloud budget for this matrix has been admitted.

The request key is `(scenario, window_role, arrival_id)`; arrival IDs and timestamps intentionally match across scenarios. Rows pin Prompt ID/hash, Trace ID/hash, data version, source arrival, stage and both seeds. Capture files and input datasets are verified before replay. The sampled Copilot rate is not full-product QPS.

## Commands on wlh-190

```bash
.venv/bin/python -m experiments.exp1.data.prepare_e1b --output results/phase5/workload-v1
.venv/bin/python -m unittest tests.test_e1b -v
docker build --network=none --pull=false \
  -f benchmarks/DataSciBench/nodes/AgentTools/Dockerfile \
  -t wabber-agent-sandbox:phase5 benchmarks/DataSciBench/nodes
.venv/bin/python -m experiments.exp1.runner.run_e1b_local \
  --workload results/phase5/workload-v1 --scenario fixed --role low \
  --family mcp --image wabber-agent-sandbox:phase5 \
  --prefix-ms 60000 --output results/phase5/local-fixed-mcp-example
```

Use a new output directory for every run. Omitting `--prefix-ms` runs the full original ten-minute window locally. A prefix is explicitly validation-only, retains the original initial idle period and arrival intervals, and never compresses the time axis. `--family faas` validates the other lifecycle. The local runner accepts no cloud endpoint/provider and uses no live LLM calls. It resolves the existing image to its immutable image ID without downloading images or installing dependencies.

The runner reuses Phase 2 LangGraph Replay, ATSuite adapters, the Docker lifecycle fixture, Phase 3 open-loop scheduler and Phase 4 metric extraction. Every MCP Session receives its own frontend process and resident calculation kernel. FaaS performs real state restoration and persistence. Concurrent sessions therefore do not model production MCP as a shared process. Local frontend startup is included in validation latency; these numbers do not stand in for cloud performance. Sandbox workers retain the existing network, credential and filesystem isolation. All terminal failures are preserved; failed local runs return a nonzero CLI status.

## Analysis

```bash
.venv/bin/python -m experiments.exp1.analysis.compare_composition \
  --workload results/phase5/workload-v1 \
  --output results/phase5/comparison.json \
  results/phase5/local-fixed-mcp-example OTHER_MATCHED_RUN_DIRECTORIES
```

Comparison verifies sealed assignments, all terminal records, and identical arrivals within each compared stage. It reports stage p95, success/SLO rates, CPU/RSS/synchronization observations and coverage counts. Deltas use the fixed-composition control for the same provider, traffic window and stage. Cross-stage p95 changes alone cannot establish a composition effect because real traffic also changes. The resource groups overlap, so changes are composition effects, not an isolated causal estimate of CPU versus memory pressure. A single seed does not justify multi-seed confidence intervals.

Costs remain null until actual billing is supplied. The optional `--billing` input reuses E1-A's actual vendor line reconciliation, raw-evidence hashes, duplicate detection, conservative allocation and full component coverage. Stage allocation keys are `<repository-relative-run-directory>#stage=<index>`; document how shared idle/storage charges are allocated and conserve each vendor line across stages. Local validation rejects cloud cost assignments. An oracle result requires all four providers, complete actual costs and success >=99%, SLO attainment >=95%. Tied winners are retained; a switch is recorded only between disjoint feasible winner sets. Missing evidence leaves the switch unknown.

Before enabling cloud E1-B, extend the existing frozen-code image policy to all selected captures, check architecture compatibility (the known `human_12` ARM prediction divergence is unresolved), and obtain a new multi-task Pilot and budget admission. The single-task E1-A images and admission must not be reused as if they support all six tasks.

The older `capture6` image reproduces outputs but predates worker CPU/RSS instrumentation. Retain those runs as diagnostics; use the locally rebuilt `phase5` image for measured validation. No image is pushed to a registry.

## Completed local validation

Baseline code was committed as `7a016b1` before Phase 5 development. `results/phase5/workload-v1` contains the 2,166-row sealed request table. Full regression passed 120 tests; the final Phase 5 suite passed 6 tests, followed by the final analysis-specific check. The rebuilt image passed all six tasks on both lifecycles (12/12 real replays), including CPU/RSS coverage, state continuity and output comparison. Three scenarios on both lifecycles passed 24/24 requests on the original 60-second low-window prefix, with two concurrent Sessions and zero submission loss in each run. These prefix tests cover stage 0; all three stage assignments are checked in the planner tests, but full ten-minute mixed runs were not executed.

Final paired local report: `results/phase5/composition-local-final.json`. Validation counts, image ID and evidence hashes: `results/phase5/validation-summary.json`. Initial image-tag and missing-metric diagnostics remain preserved. Local cost and strategy-switch results are deliberately unavailable.
