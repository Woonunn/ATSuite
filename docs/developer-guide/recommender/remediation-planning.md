# FIX-02 and FIX-03 remediation evidence

Start HEAD: `de752f21342162dd4412ea9dd3b6e224a1f633fd`; current branch preserved.
Implementation is recorded in local commits d89dd27 and d7b6bd9, with provider
execution integration in b12b011. No branch switch, reset, rebase, stash or push
was performed. Combined final validation is recorded in remediation-status.md.

## Reproduction

The first project-environment run before implementation was:

```sh
UV_CACHE_DIR=/tmp/atsuite-uv-cache uv run --no-sync python -m unittest \
  tests.test_recommender_topology_validation tests.test_recommender_resource_search -v
```

Result: 9 tests, 5 failures and 4 errors. It reproduced missing validity gates,
unsafe unknown-state splitting, non-deduplicated deployment groups, lost bundle
data, discarded multiple state domains, overwritten original resources,
provider coverage starvation, accepted illegal fractional GCP resources, rejected
legal Lambda configurations outside the sampled grid, and imputed missing
resources. The no-sync flag used the actual project environment while the root
agent's locked dependency synchronization was still running; it did not bypass
the project with no-project.

## FIX-02 implementation

Topology plans expose validity, blocking reasons, warnings, and all generating
strategies. Invalid plans are excluded, and compilation repeats route,
compatibility, and state-atom checks. Must-colocate and local-state relationships
form indivisible atoms. Unknown state preserves related original groups.
Multiple state domains/accesses, declared isolation/permissions/dependencies,
Python versions, system dependencies, and pinned package constraints survive
config parsing. Conflicting pinned dependencies/Python/isolation are blocked;
other dependency compatibility requires an isolated build probe.

Equivalent groups share a topology identity; baseline target names/build/runtime
and lifecycle settings are retained. Nodes/tools, pipeline/realizations, and the
legacy session alias normalize to one config. Duplicate routes, unknown nodes,
and ambiguous formats are rejected. Config resolution reads current files and
retains the old cache-clear API without stale path-only caching.

Materialization copies the full source manifest with file modes, trace, nodes,
data, and implicit init files into each candidate's bundle. It verifies source
SHA hashes before publication, creates separate normalized source and generated
config hashes, rejects existing plan/bundle outputs, and leaves source config
unchanged. Generated configs live under bundle/config so normal benchmark root
lookup continues to find data. Child proposals use frozen parent bundle content,
retain the workload fingerprint and parent plan digest, revalidate ordinary
state/topology/resource rules, and never mutate an approved parent.

Logical state domains remain stable when generated target names change.
Deployment environment, initial snapshot aliases, session attachment, FaaS seed
keys, and cleanup use the same logical domain and SDK StateKey constructor.
GCP session snapshots attach once; Alibaba and GCP FaaS snapshot seeding runs
alongside Lambda, propagating storage errors. Baseline processes with several
state domains retain their original namespace; ambiguous regrouping is excluded
with `ambiguous_state_regrouping` instead of selecting a domain. Real workflow
replay uncovered mutable SDK default-state leakage between UIDs; restoring a
deep copy prevents trial state from mutating frozen defaults or snapshots.

## FIX-03 implementation

Capability snapshots distinguish actual service/deployment mode, region,
architecture, resource semantics, supported ranges/steps, sparse search samples,
rule source/version/date, execution mode, and billing mode. Planning never
returns invented effective allocation. Lambda CPU is derived from observed
memory; AgentCore allocations remain provider managed. The existing
ali_agentrun adapter is accurately identified as FC custom-container MCP session
mode, rather than conflated with the newer AgentRun API.

Exact requested baselines are recorded per provider/family, including rejected
original values and exclusion reasons. Candidate generation changes one target
at a time, preserving heterogeneous peers and their timeout/disk/lifecycle
options. Provider/family round-robin occurs before extra allocation. Uncovered
providers are explicitly skipped_by_budget/coverage_insufficient. Adaptive
Advisor search reserves a slot only when provider coverage permits; a frozen
withheld ordinary candidate supplies deterministic fallback when no model is
available.

Region, architecture, concurrency, billing, execution environment, minimum
instances, and supported session lifecycle options survive runtime resolution
and provider calls. Strict experiment adapters refuse conflicting ownership or
image, attach ownership metadata, propagate isolated state environment, and
avoid requested-as-effective readbacks. Lambda verifies existing X-Ray role
permissions without attaching IAM policies. Cloud Run private endpoints use ID
tokens. Alibaba strict deployment does not create the global SLS project or
copy host OSS credentials; optional logging uses a pre-provisioned log project
and logstore. Provider API errors are propagated.

Official rules checked on 2026-10-08:

- [Cloud Run CPU](https://docs.cloud.google.com/run/docs/configuring/services/cpu)
  and [memory](https://docs.cloud.google.com/run/docs/configuring/services/memory-limits):
  fractional memory caps and concurrency/request billing/gen1 requirements.
- [Lambda memory](https://docs.aws.amazon.com/lambda/latest/dg/configuration-memory.html)
  and [ephemeral storage](https://docs.aws.amazon.com/lambda/latest/dg/configuration-ephemeral-storage.html):
  full integer ranges and memory-derived CPU, separate from search samples.
- [Alibaba FC specifications](https://www.alibabacloud.com/help/en/functioncompute/instance-types-and-specifications):
  CPU/memory steps, allowed ratio, and disk values.
- [AgentCore microVMs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html):
  managed allocation, separate from explicitly configurable instance compute.

## Verification and limits

A locked project-environment run completed after synchronization (uv macOS
system-configuration panic required the approved outside-sandbox retry):

```sh
UV_CACHE_DIR=/tmp/atsuite-uv-cache uv run --offline python -m unittest \
  tests.test_recommender_provider_options tests.test_recommender_topology_validation \
  tests.test_recommender_resource_search tests.test_runtime_refactor \
  tests.test_build_cli tests.test_recommender_platform.RecommenderPlatformTests -v
```

Result: **42 passed**. Provider tests fake only cloud clients/process boundaries
and inspect real adapter API payloads/readback normalization; they do not claim
live cloud verification. The final scoped project-environment run completed
**53 passed**, including immutable child planning, logical state routing,
snapshot seeding/error contracts, and real deterministic SDK workflow replay
across all three FaaS provider routes. Only cloud transports and object stores
are substituted in those local integration tests; actual registered benchmark
tools produce the oracle outputs and state. The results are recorded in
[planning-regressions.txt](evidence/planning-regressions.txt). New snapshot tests
first reproduced two failures (missing GCP session seeding and Alibaba append
without replacement); expanded real replay also reproduced cross-UID default
state corruption before the deep-copy correction.
The root agent owns final integration, browser E2E, and combined regression
verification after all shared files settle.

| Capability | implemented | unit_tested | integration_tested | browser_e2e_tested | cloud_verified | model_evaluated |
| --- | --- | --- | --- | --- | --- | --- |
| FIX-02 topology/bundle | yes | yes | real local config/bundle resolution | root-owned | blocked_external | n/a |
| FIX-03 resources/adapters | yes | yes | fake external provider contracts | root-owned | blocked_external | n/a |
| Immutable child materialization | yes | yes | frozen parent → child config | root-owned | blocked_external | n/a |
| RecommenderOracle | yes | yes | real SDK output/state transactions and workflow replay | root-owned | blocked_external | n/a |
| Logical state routing | yes | yes | original snapshot aliases → SDK keys → cleanup | root-owned | blocked_external | n/a |

Multiple-domain process regrouping is conservatively unavailable until explicit
per-module snapshot composition is implemented; exact baselines remain valid.
The controlled counter workload verifies both stable logical-domain routing and
physical target rename behavior. Ordinary legacy invocation without experiment
state_identity still uses legacy seed/cleanup object paths; the recommender
execution service supplies the full StateKey identity.
Cloud accounts, registry/API execution, real billing, provider region acceptance,
and live resource cleanup remain **blocked_external** until executed with
bound approval. No synthetic, fixture, or predicted result was declared cloud
verified.
