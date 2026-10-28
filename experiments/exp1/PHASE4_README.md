# E1-A: real traffic sensitivity

Status: all four final cloud Pilots passed (three Sessions each). The single-seed formal matrix is running; complete vendor billing remains pending.

## Frozen workload

`configs/e1a.yaml` selects the valid `csv_excel_34` capture (Census income, Gini and Lorenz curve), one sequence seed **11**, and computation seed **0** from that capture. The original captured programs and all computation parameters are unchanged. UUIDs identify executions; they do not choose workloads. There are no multiple-seed confidence intervals.

`human_12` was considered first but excluded from this paired experiment after a real AgentCore Pilot: the ARM64 random forest changed 39/16,281 predicted classes relative to the x86 capture. The mismatch was reproduced in offline Docker; no numerical tolerance was widened to accept it. The original capture and failed Pilot remain intact.

For `csv_excel_34`, x86 Docker reproduced all captured outputs exactly. ARM reproduced cleaning, Gini, state signatures and independent result checks; its PNG differs in 393/1,102,500 pixels, by at most one of 255 channel levels. The two exact decoded-RGBA digests are sealed in `configs/e1a_rendering_equivalence.json`, with hashed offline evidence. Only this calibrated rendering alternative is accepted; arbitrary images or numerical differences still fail. Merge fingerprint metadata follows the actual validated artifact, while Python code, data, dependencies and numerical parameters are identical. Every use of this rendering equivalence is logged.

All four providers execute the same immutable logical request table. The selected real ten-minute Copilot windows contain 53, 191, and 478 arrivals: 722 per provider, 2,888 in total. They were selected before provider results using the Phase 3 rule. Absolute arrival rates describe sampled production Sessions, not full-product QPS. Formal replay uses `time_scale=1.0` and preserves timestamp ties.

```bash
uv run python -m experiments.exp1.data.prepare_e1a --output results/phase4/workload-csv34-v10
uv run python -m experiments.exp1.runner.deploy_e1a --deploy --output results/phase4/deployment
uv run python -m experiments.exp1.runner.run_e1a --workload results/phase4/workload-csv34-v10 \
  --deployment results/phase4/deployment/deployment.json --pilot --output results/phase4/pilot
```

Builds and provider deploy commands run concurrently in separate directories. Configuration readback uses the main interpreter thread because the Alibaba credential SDK installs signal handlers. Deployment records include source hashes, image identity, actual API settings and requested settings that the provider cannot enforce. Remote `prepare_workspace` reports its implementation SHA and the Harness verifies it against the audited image. No prewarming requests are issued.

## Execution lifecycle

MCP retains its canonical Python heap and files inside one Session environment. Read-only analysis branches fork from that heap; successful merges commit new shared state. A subsequent MCP Tool call does not reload a snapshot.

FaaS restores registered state and files for each invocation, persists writes, and uses object-store conditional locks for conflicting writes. Read-only calls do not commit. A warm instance may reuse an otherwise stateless Python dispatcher and imported dependencies; each analysis still restores its supplied state. Invocation-local workspaces are removed after persistence so completed Sessions cannot accumulate beyond the platform disk quota. This is an explicit opt-in, not a change to legacy workloads.

The cloud images contain the prepared dataset and pinned dependencies. They perform no business-network downloads or package installation during requests. Cloud state RPC uses S3 or same-region OSS internal endpoints.

### Closed-program cloud replay

The actual AgentCore environment returned `ENOSYS` for Landlock. Cloud E1-A therefore uses the same explicit `frozen_trace_only` policy on all four providers:

* The image contains an allowlist of the reviewed original program hashes, task, input hashes, step IDs, output paths, snapshots, and seeds. Modified programs or parameters are rejected.
* External state-bootstrap payloads are disabled, including pickle injection through the legacy bootstrap interface.
* A clean worker excludes cloud credentials and SDK descriptors. AgentCore and Alibaba install nested seccomp filters. Lambda blocks `capset` and `prctl`; its explicit closed-program policy verifies already-empty capabilities and inherited platform seccomp, uses the provider microVM, and retains Python file/network/process audit rules and resource/time limits. It does not claim to install its own seccomp filter. Unapproved code and external state injection remain rejected on every provider.
* This policy supports these fixed reviewed programs. Python audit hooks are not a security boundary for arbitrary new Python. Live capture of newly generated code continues to use Docker isolation.

The resident dispatcher blocks on a private control pipe and SIGCHLD notifications rather than polling while idle. This avoids keeping an instance above the platform CPU threshold solely to wait for work. The pipe descriptors contain no SDK credentials and are closed in analysis children. Startup is capped at 60 seconds; each program at 180 seconds; the provider/Tool RPC timeout is 240 seconds.

The outer native process prepares isolation; actual numerical computation stays inside the ATSuite Tool worker. `sandbox_worker.py` records measured CPU time and RSS. No artificial CPU loop or wait is inserted into task computation.

ARM emulation diagnostics, when used, run offline in a read-only Docker container with dropped capabilities. QEMU cannot install the guest seccomp filter; its diagnostic-only copy of the launcher omits that filter. This is not a production sandbox test and is not used for performance measurements.

## Resource differences

Requested policy: 1 CPU, 2,048 MB memory, 512 MB disk, 240-second Tool timeout; Harness concurrency 64. There is no resource search.

* Lambda has one invocation per instance and reserved request concurrency 64. CPU follows memory; the requested CPU value cannot independently configure it. Python 3.12 uses AL2023 so the pinned numerical wheels are supported.
* AgentCore uses ARM64 and provider-managed CPU/memory. The installed API exposes neither CPU nor memory setters; their actual values remain unknown unless observed from the runtime. Session idle timeout is 60 seconds, maximum lifetime 900 seconds. Session usage-log delivery is explicitly enabled.
* Alibaba FC uses 2 CPU, 4,096 MB, 512 MB disk, invocation concurrency one, and reserved request concurrency 64. Reserved concurrency is not mislabeled as a maximum instance count.
* The existing ATSuite `ali_agentrun` adapter deploys an FC Session-exclusive MCP service, not the separate newer AgentRun API. Its instance request-concurrency setting is fixed at 200 by the platform; the captured graph still dispatches at most two independent branches concurrently within one Session environment, one Session per instance, 60-second idle timeout and 900-second TTL. The deployment report labels this distinction.

The initial 4-GB Pilot observed roughly 400 MB peak function memory. The fixed 1-CPU/2-GB policy was chosen once before formal execution; there is no resource search. The worker address-space ceiling is 3 GB of virtual memory, distinct from the provider's 2-GB physical allocation. This workload reuses committed files and small canonical metadata; it does not demonstrate a large resident DataFrame cache.

Actual limits, architecture, code version and confinement mode are retained in per-request results. Differences are not hidden behind nominally equal requested resources.

## Pilot and budget admission

The user-approved total spending limits are Alibaba **CNY 15** and AWS **CNY 30**, including Pilot, runtime, state, logs, registry and network. The AWS admission ceiling is USD 4.49, rounded down from CNY 30 at the frozen ECB reference rate dated 2026-10-26 (USD/CNY 6.668126564...). Both pools retain a 10% reserve. The reference is not a bank settlement rate. `budget_ledger.json` pools both strategies for each vendor. Reservations are not invoices; actual charges stay null until supported by vendor records. Closed-Pilot planning bounds can be revised using immutable usage/pricing evidence; prior amounts, reasons and evidence are retained.

Pilot sends three real Sessions using the beginning of the low window without changing their relative offsets. It checks task outputs, workspace continuity, overlapping execution, failures, resource settings, and collection access. It is explicitly a Pilot, not a shortened formal window. Failure stops matrix progression. A formal run requires a matching, evidence-hashed admission record and a successful Pilot. Large queues, failed calls and timed-out calls remain in the scheduler log; they are not silently discarded.

```bash
uv run python -m experiments.exp1.runner.run_e1a --workload results/phase4/workload-csv34-v10 \
  --deployment results/phase4/deployment/deployment.json --admission results/phase4/admission.json \
  --output results/phase4/formal
```

The matrix follows the provider order sealed in the workload manifest, separately shuffled for each window by seed 11. Each run holds one static provider for the entire arrival window and drains admitted requests afterwards.

## Measurements and billing

`result_collector.py RUN` produces request JSONL and a window report from scheduler terminals and actual Tool responses. The provider collector adds CSV and platform metrics after ingestion. Missing measurements remain null with coverage counts. Per-request endpoints include planned arrival, actual submission, Harness start and final completion. Harness queue delay and provider queue delay are separate. Alibaba scheduling latency includes cold-start and scheduling work, so it is reported separately rather than mislabeled as pure queue delay. TLS/connection errors retain their original diagnostics.

Active Tool path is the union of client Tool intervals; summed Tool execution and worker computation are reported separately. Actual parallelism uses worker execution intervals only when clocks have the same resident worker instance identity. RPC overlap is a different metric. Worker RSS is a process high-water mark; canonical resident work can include earlier calls. Application process-first-task markers are distinct from provider cold starts.

```bash
uv run python -m experiments.exp1.runner.result_collector RUN
uv run python -m experiments.exp1.runner.result_collector RUN --provider
uv run python -m experiments.exp1.runner.billing --help
```

AWS collection waits at least 30 minutes after completion; AgentCore usage delivery can take up to 60 minutes. Early diagnostics are labeled separately. Cost Explorer can lag at least a day. AWS usage logs are monitoring evidence, not an actual invoice. Account-wide daily totals are not automatically attributed to E1-A. Each collection writes a new immutable evidence file; a separate status index points to the latest snapshot. Admission proofs also snapshot their inputs so later telemetry enrichment cannot invalidate a running matrix. Alibaba bills must likewise match the actual function/session and billing period. Current FC pricing includes idle-memory/instance-release costs and a per-function hourly minimum; it is incorrect to sum only Tool CPU durations. MCP initialize/notification requests can also be billable. Instance/session charges must be allocated once, including idle tails crossing window boundaries.

Normalized actual bill lines retain raw-file hashes, vendor record IDs, cost categories and allocation bases. Duplicate lines and allocations that do not conserve charges are rejected. Session fees are counted once per Session, not once per Tool. Complete costs require runtime, state storage, state requests, logs, registry and network coverage. Currency conversion requires explicit sourced evidence. Empty bill responses do not establish zero cost.

## Comparison and figures

`analysis/compare_strategies.py` requires paired providers with the same logical rows. An eligible strategy has success rate at least 99% and SLO attainment at least 95% (SLO 120 seconds, fixed in config). The cost oracle only selects among strategies with complete actual billing. It never invents a winner while fees are missing.

`analysis/plot_e1a.py` draws publication-style PDF/PNG figures with English labels: p95 latency versus sampled arrival rate, actual cost per successful request versus rate, SLO attainment, and cost–latency tradeoffs for the three windows. Missing costs are labeled. A change in the best provider is an empirical result, not a plotting or window-selection requirement. Output belongs in `figure/e1a/`.

The current admitted plan is `results/phase4/admission-v2.json`: formal-only upper planning reservations USD 3.239481 for AWS and CNY 11.304091 for Alibaba, plus completed-Pilot bounds and the separate reserve. These are budget forecasts, not actual cloud expense reports. `runner/calibrate_e1a.py` reproduces the arithmetic and binds the exact workload manifest, provider configs, observed Pilot data and immutable evidence snapshots.

The initial run `results/phase4/e1a-formal-seed11-v1` is stopped at the user's request pending a US-east driver host, and retained as a transport diagnostic rather than the primary comparison. Its low-window AWS failures were TLS EOF errors (Lambda 9/53, AgentCore 16/53); the cross-border route is suspected but unproven. TLS-only spot checks passed 20/20 for each AWS endpoint. `diagnostic-run.json` preserves interruption counts and unknown remote outcomes; `tls-handshake-diagnostic.json` preserves the connection-only checks. No further experiment requests are authorized until the user resumes. Deployment and resource settings were unchanged during this run. Re-running a failed matrix must use a new directory and retain all earlier failures. The same seed does not justify removing failed attempts.
