# E1-B: Task comparison under fixed typical traffic

The four strategies are `aws_lambda`, `aws_agentcore`, `ali_fc`, and `ali_agentrun` (the existing FC Session-exclusive MCP adapter). Cloud preparation was authorized on 2026-10-29, with no spending cap and an instruction to minimize costs. Actual execution status is recorded in the run matrices; preparation is not evidence of a successful cloud experiment.

## Measured classes

`configs/e1b.yaml` fixes thresholds before observing provider outcomes. Labels may overlap:

- CPU-intensive: median CPU time >= 5 seconds and CPU time / wall time >= 0.8.
- Memory-intensive: maximum process RSS >= 768 MiB.
- State-intensive: maximum serialized reference state >= 64 MiB.
- Relatively lightweight: median wall time < 5 seconds, RSS < 768 MiB and reference state < 64 MiB.

This classifies `human_142` as CPU-intensive; `human_12` and `human_7` as CPU-, memory- and state-intensive; `human_15`, `csv_excel_34` and `csv_excel_29` as relatively lightweight. These are operational thresholds for the six measured candidates, not universal resource classes. Full measurements and environment are pinned from the Phase 1 task manifest. Phase 1 reference programs differ from captured Agent programs: reference pickle size is not measured Agent state-transfer cost. Replay records actual worker CPU, RSS and SDK synchronization separately, with missing observations left null.

## Current experiment configuration

Only `fixed` on the previously selected `typical` ten-minute Copilot window is enabled, with 191 arrivals. The original arrival timestamps and intervals are retained, not replaced with constant-rate traffic. The window retains three half-open reporting stages: [0,200), [200,400), [400,600) seconds, containing 55/66/70 requests. Seed 11 fixes task allocation and provider order; captured computation parameters retain seed 0.

`fixed` assigns equal weights to all six tasks throughout the window. CPU and memory/state ramps and the low/high windows are no longer part of the active configuration. Queue capacity remains 1,000 and latency SLO remains 120 seconds.

Largest-remainder quotas followed by a seeded shuffle retain exact request counts and approximately match equal proportions despite integer rounding. Actual per-stage task counts are stored. The new `results/phase5/workload-fixed-typical-v1` table has 191 rows. Each provider must run the same task assignments and arrivals, yielding 764 executions across four ten-minute windows, before Pilot runs or repetitions. The historical `workload-v1` and its results remain sealed under the former three-scenario, three-window configuration; editing YAML does not change an existing workload. Its historical cloud-admission status predates the new authorization, which is recorded separately in `results/phase5/cloud-fixed-typical-v1/deployment-config.yaml` and each new cloud run.

The request key is `(scenario, window_role, arrival_id)`. Rows pin Prompt ID/hash, Trace ID/hash, data version, source arrival, stage and both seeds. Capture files and input datasets are verified before replay. The sampled Copilot rate is not full-product QPS.

## Commands on wlh-190

```bash
.venv/bin/python -m experiments.exp1.data.prepare_e1b --output results/phase5/workload-fixed-typical-v1
.venv/bin/python -m unittest tests.test_e1b -v
docker build --network=none --pull=false \
  -f benchmarks/DataSciBench/nodes/AgentTools/Dockerfile \
  -t wabber-agent-sandbox:phase5 benchmarks/DataSciBench/nodes
.venv/bin/python -m experiments.exp1.runner.run_e1b_local \
  --workload results/phase5/workload-fixed-typical-v1 --scenario fixed --role typical \
  --family mcp --image wabber-agent-sandbox:phase5 \
  --concurrency 2 --output results/phase5/local-fixed-typical-mcp-full-v1
```

Prepare the workload only if its output directory does not already exist. Use a new output directory for every run. The command above runs the full original ten-minute arrival window locally, followed by any remaining execution/queue drain. An optional `--prefix-ms` selects a validation prefix, retains the original initial idle period and arrival intervals, and never compresses the time axis. `--family faas` validates the other lifecycle. The default local concurrency is 2; this is not a cloud concurrency setting. The local runner accepts no cloud endpoint/provider and uses no live LLM calls. It resolves the existing image to its immutable image ID without downloading images or installing dependencies.

The runner reuses Phase 2 LangGraph Replay, ATSuite adapters, the Docker lifecycle fixture, Phase 3 open-loop scheduler and Phase 4 metric extraction. Every MCP Session receives its own frontend process and resident calculation kernel. FaaS performs real state restoration and persistence. Concurrent sessions therefore do not model production MCP as a shared process. Local frontend startup is included in validation latency; these numbers do not stand in for cloud performance. Sandbox workers retain the existing network, credential and filesystem isolation. All terminal failures are preserved; failed local runs return a nonzero CLI status.

## Analysis

```bash
.venv/bin/python -m experiments.exp1.analysis.compare_composition \
  --workload results/phase5/workload-fixed-typical-v1 \
  --output results/phase5/comparison-fixed-typical.json \
  results/phase5/local-fixed-typical-mcp-full-v1 OTHER_MATCHED_RUN_DIRECTORIES
```

Comparison verifies sealed assignments, all terminal records, and identical arrivals within each compared stage. It reports stage p95, success/SLO rates, CPU/RSS/synchronization observations and coverage counts. Under the fixed-only configuration, deltas against the same provider's fixed control are zero when defined; compare providers directly. These stage aggregates describe the mixed workload. For task-specific comparisons, use `task_id` in each run's `request_metrics.jsonl` to compare the same task and arrival IDs across providers. Different tasks share the mixed run and do not each receive a separate 191-request window. Cross-stage changes alone cannot establish a task effect because real traffic also changes. The resource groups overlap, so they do not identify an isolated CPU versus memory effect. A single seed does not justify multi-seed confidence intervals.

Costs remain null until actual billing is supplied. The optional `--billing` input reuses E1-A's actual vendor line reconciliation, raw-evidence hashes, duplicate detection, conservative allocation and full component coverage. Stage allocation keys are `<repository-relative-run-directory>#stage=<index>`; document how shared idle/storage charges are allocated and conserve each vendor line across stages. Local validation rejects cloud cost assignments. An oracle result requires all four providers, complete actual costs and success >=99%, SLO attainment >=95%. Tied winners are retained; a switch is recorded only between disjoint feasible winner sets. Missing evidence leaves the switch unknown.

## Cloud preparation and execution

`deploy_e1a` also accepts the E1-B deployment configuration and creates separately named `E1b*` targets. Setting its `workload` field makes the build verify and bake all six sealed tasks, input datasets and per-task code/parameter allowlists. The original single-task E1-A policy remains supported. E1-B adds the captured programs' CPU PyTorch, Excel and mlxtend dependencies. Images are audited before deployment.

The current deployment requests 2 CPU, 4 GiB memory and a 240-second Tool timeout. Lambda requests 2 GiB temporary disk; Alibaba uses its supported 10 GiB tier because its only smaller tier is 512 MiB. Lambda CPU scales with configured memory; AgentCore resources are provider-managed. Deployment readback records what was actually applied. Formal harness concurrency is 64 and the queue capacity remains 1,000. MCP retains canonical state inside its Session; FaaS restores/persists through object storage.

`run_e1b_cloud` requires all four audited deployments. `--pilot` selects the first original arrival of each task and runs tasks serially, stopping paid submissions after a failure. The formal run requires all 24 task/provider Pilot outcomes to succeed with the correct state lifecycle. It then runs providers sequentially in the sealed order. Each admitted formal window runs once; no live LLM calls are made. Missing billing remains unknown. The existing evidence-backed `csv_excel_34` PNG rounding calibration is checked and reused for original references. Replay remains strict against the selected reference for every task; cross-environment equality is not assumed.

```bash
.venv/bin/python -m experiments.exp1.runner.run_e1b_cloud \
  --workload results/phase5/workload-fixed-typical-v1 \
  --deployment PATH_TO_VERIFIED_DEPLOYMENT_JSON \
  --config results/phase5/cloud-fixed-typical-v1/deployment-config.yaml \
  --pilot --output results/phase5/cloud-fixed-typical-v1/pilot
# After successful Pilot and lifecycle validation:
.venv/bin/python -m experiments.exp1.runner.run_e1b_cloud \
  --workload results/phase5/workload-fixed-typical-v1 \
  --deployment PATH_TO_VERIFIED_DEPLOYMENT_JSON \
  --config results/phase5/cloud-fixed-typical-v1/deployment-config.yaml \
  --pilots results/phase5/cloud-fixed-typical-v1/pilot \
  --output results/phase5/cloud-fixed-typical-v1/formal
```

The older local `capture6` image reproduces outputs but predates worker CPU/RSS instrumentation. Retain those runs as diagnostics; use a measured image for new validation. Historical local validation did not push images to a registry; cloud deployment does.

## Historical local validation (former configuration)

Baseline code was committed as `7a016b1` before Phase 5 development. `results/phase5/workload-v1` contains the 2,166-row sealed request table. Full regression passed 120 tests; the final Phase 5 suite passed 6 tests, followed by the final analysis-specific check. The rebuilt image passed all six tasks on both lifecycles (12/12 real replays), including CPU/RSS coverage, state continuity and output comparison. Three scenarios on both lifecycles passed 24/24 requests on the original 60-second low-window prefix, with two concurrent Sessions and zero submission loss in each run. These prefix tests cover stage 0; all three stage assignments are checked in the planner tests, but full ten-minute mixed runs were not executed.

Final paired local report: `results/phase5/composition-local-final.json`. Validation counts, image ID and evidence hashes: `results/phase5/validation-summary.json`. Initial image-tag and missing-metric diagnostics remain preserved. Local cost and strategy-switch results are deliberately unavailable.

## Environment-specific strict references

The user approved separate environment references while retaining all six tasks after the Lambda LSTM Pilot diverged from the original Capture. Local reproduction showed the same frozen program, input and initial weights yielding different final weights/loss under different CPU numeric kernels (`results/phase5/lambda-lstm-fix-v1/diagnosis.json`). Original Capture files and workload assignments remain unchanged.

`calibrate_e1b` runs original frozen decisions, code, seeds and recorded model waits on the audited deployment. It permits output signatures and numeric checks to differ while preserving Tool identities, parameters, artifact names, snapshots and success/error control flow. Independent task validation must pass before a candidate reference is sealed. The derived trace records actual model input messages and actual Tool results. No live LLM calls or numerical tolerances are added. `differences.json` records original versus environment output evidence separately.

Pass the sealed `references.json` with `--references` to **both** Pilot and formal `run_e1b_cloud` invocations. Every candidate must pass a separate strict replay in a fresh Pilot Session. Formal admission checks the exact reference seals used by each provider's Pilot, as well as the original workload, deployment and lifecycle evidence. `--reuse-pilots OLD_PILOT_DIRECTORY` is available only in Pilot mode and reuses a provider only if all six tasks and all reference/deployment checks already pass. Previous failed traces are retained.

The current recovery launch is `results/phase5/cloud-fixed-typical-envref-v3/launch.py`; it preserves the sealed Lambda LSTM and six AgentCore candidates, adds Lambda references for the four remaining tasks after `human_7` CSV digests also differed, reuses both successful Alibaba Pilots, strictly replays both AWS Pilots, then admits the unchanged 191-request window per provider. Lambda `csv_excel_34` continues to use the original reference, which passed strict Pilot. Lambda LSTM passed its first strict environment-reference Pilot before the subsequent CSV divergence stopped that matrix. The earlier recovery stopped when AgentCore lost its Session process during the long `human_7` preparation call. AgentCore idle timeout was increased from 60 to 300 seconds, exceeding the 240-second Tool deadline; maximum lifetime remains 900 seconds. The lifecycle change and cloud readback are recorded in the new deployment manifest. Pilot reuse and reference reuse compare each provider’s complete deployment entry, so an AgentCore-only update does not invalidate unchanged Alibaba or Lambda evidence. The candidate references are bound to the audited deployment; they are not a guarantee against hardware variation within a provider. Any later strict divergence still fails the run. Performance comparisons use task-equivalent independently validated computations; cross-environment numerical differences are reported separately, not presented as byte-identical results. Actual billing remains pending until vendor evidence is collected.

Recovery validation on 2026-10-29: all 24 cloud Pilot tasks passed, including strict replays of the sealed environment references and the MCP/FaaS lifecycle checks. Formal replay started in `results/phase5/cloud-fixed-typical-envref-v3/formal`; its admission file seals 32 Pilot evidence files. The original workload manifest SHA-256 remains `b21455c238d6604ce7ce15ba61e78993b5f2c0280f402d95f4635c448e712c99`. All 59 relevant unit tests passed (`cloud-fixed-typical-envref-v2/tests-final.log`). Formal completion and actual billing must be read from the run artifacts rather than inferred from successful Pilot.
