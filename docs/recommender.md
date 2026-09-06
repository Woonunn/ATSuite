# Automatic Deployment Recommendation

ATSuite Recommender chooses a **deployment strategy: cloud + runtime family + configured deployment shape** from repeated benchmark measurements. It never predicts a provider from static workload rules. State access semantics describe the workload; they have no scoring weight.

See also [中文说明](recommender.zh.md).

## Run a comparison

Install the provider dependencies you intend to use, configure their credentials, registries, storage and observability as for normal build/deploy/replay, and start Docker:

```bash
uv sync --group aliyun --group aws --group gcp
uv run atsuite-recommend \
  --config benchmarks/TravelPlanner/config/faas7_mcp2_min.json \
  --runs 5 \
  --objective balanced \
  --fx-config docs/recommender_fx.example.json
```

The example CNY rate is a reproducible **example input**, not a current exchange-rate quote. Replace it with your chosen fixed rate before a real comparison. Provider setup: [Alibaba](userguide/ali.md), [AWS](userguide/aws.md), [GCP](userguide/gcp.md).

This command actually builds images, deploys the configured targets, and invokes cloud services. Normal deployment credentials, permissions and charges apply. It uses the existing deployment names and can update deployments with those names. It does not resize resources, change target grouping, or search CPU/memory combinations.

## Candidates and execution

| Cloud | FaaS | Session-MCP |
|---|---|---|
| Alibaba | `ali_fc` | `ali_agentrun` |
| AWS | `aws_lambda` | `aws_agentcore` |
| GCP | `gcp_faas` | `gcp_mcp` |

Candidates come from `atsuite.pipeline.PROVIDERS` and each is checked with `resolve_benchmark()`. A FaaS-only config produces unsupported Session-MCP candidates with retained reasons; `pipeline.session` and its existing `pipeline.mcp_serverless` alias are supported by the resolver. Unsupported candidates and individual build/deploy/run failures do not abort other candidates.

`mcp_gateway` is excluded by default. Opt in with `--include-provider mcp_gateway`, or name it in `--providers`. Its current pricing policy supplies no comparable cost evidence, so cost remains unavailable.

Every supported requested candidate goes through:

```text
validate → build → deploy → warmup → repeated measured runs
         → collect AnalysisReports → aggregate → cleanup attempt
```

The runner calls Python APIs (`build_resolved_targets`, `deploy`, `run_trace`, `Analyzer`), without spawning ATSuite CLI commands. Docker and provider tools used internally by those existing APIs still run normally.

Defaults are **one warmup and five measured runs**; `--runs` must be at least **3** and `--warmup-runs` can be zero. Warmups never enter scored statistics or failure rates. Each warmup and measured run gets an independent experiment/provider/run UID. Runs are sequential, with the recorded candidate order, and preserve trace delays and simulated LLM time. This measures mostly steady-state behavior with natural cold starts; V1 does not force cold starts. Analyzer ingestion waits are retained and can be long for session usage logs.

## Metrics and statistics

The recommender consumes `AnalysisReport.summary`; existing Analyzer collectors and pricing policies remain the source of provider metrics and modeled cost. It makes no second set of cloud metric or pricing queries.

| Scoring metric | Evidence | Value used for comparison |
|---|---|---|
| `latency` | `run_user_e2e_ms` | P95 by default; `--latency-stat p50`, `p95`, or `median` |
| `cost` | `total_price` and `currency` | Median cost per successful workflow, normalized to USD |
| `reliability` | Measured run outcomes | Failed measured runs / all measured runs |
| `cold_start` | `cold_start_total`, `invocation_count` | Sum of observed cold starts / sum of invocations in successful runs |
| `memory` | `avg_memory_usage_mb` | Median of the per-workflow measured averages |
| `state_overhead` | `total_state_sync_overhead_ms` | Median per successful workflow |

Latency, cost, memory, cold-start count, state, platform and network statistics retain samples, count, mean, median, minimum, maximum, P50, P95, sample standard deviation and CV. Compute time, initialization time, framework overhead and idle ratio are also exported, without default scoring weight.

P95 uses fixed **type-7 linear interpolation**: sort samples, calculate `h=(n-1)*0.95`, and interpolate between positions `floor(h)` and `ceil(h)`. P50 equals the median. Standard deviation uses `n-1` for two or more samples, otherwise zero. `CV=stddev/mean`, with zero for an all-zero sample. Empty statistics use JSON `null`. No outliers are removed.

Continuous performance/cost statistics use successful measured workflows. All failed and warmup records are retained in `run_records`, and existing raw reports remain linked. Thus a fast failure cannot improve latency statistics. Cost is **not total cloud spend**: failed runs, warmups, builds, retained resources and idle billing outside the Analyzer's accounting window can add charges beyond the reported median successful-workflow cost.

Deployment failures have a separate status, not five invented failed runs. Timeouts, runtime/provider errors and tool `isError` results make measured workflows fail. Observability failures and cleanup errors are recorded separately; they do not by themselves turn completed workflows into failures. The report additionally exposes invocation failure rate. A candidate needs `max(2, ceil(runs*0.6))` successful runs and all requested measured outcomes to qualify.

## Availability and currency

Metrics carry `available`, `partial`, or `unavailable`. Analyzer now annotates which fields were actually observed while retaining its legacy numeric summary fields. Configured memory is not measured memory; a default cold-start count of zero is not proof of a warm run; client timing substituted for billing duration is not complete cost evidence.

Partial evidence remains visible in raw statistics but is **not scored**. V1 requires complete evidence for a scored metric across successful runs and the relevant invocations. Legacy summaries without availability metadata only supply usable workflow latency. Unknown values are never silently converted to zero.

```json
{
  "base_currency": "USD",
  "rates": {"USD": 1.0, "CNY": 0.14}
}
```

Rates mean USD per unit of source currency; `USD` is always 1. No exchange rates are fetched online. Missing CNY→USD makes Alibaba cost unavailable. The report records the exact rates in `pricing.fx_rates` and the normalized currency in `pricing.normalized_currency`.

Latency and measured-run reliability are always required. A non-balanced objective also requires its namesake metric. Every positive metric in explicit `--weights` is required. Hard constraints require complete evidence for their metric. A candidate missing any required metric is excluded. Other missing/partial metrics are removed from that candidate's weight denominator; effective weights and confidence penalties are recorded.

## Constraints, Pareto frontier and scoring

The selection order is **hard constraints → Pareto analysis/filtering → deterministic weighted scoring**. A score cannot rescue a constraint violation.

```bash
uv run atsuite-recommend \
  --config benchmarks/TravelPlanner/config/faas7_mcp2_min.json \
  --providers aws_lambda,aws_agentcore,gcp_faas,gcp_mcp \
  --cloud aws,gcp --family faas,session \
  --max-p95-ms 2000 --max-cost-usd 0.01 \
  --max-failure-rate 0.01 --max-memory-mb 1024 \
  --objective cost
```

Cloud/family and metric constraints are applied after measurement; use `--providers` to restrict which deployments actually run. `--max-p95-ms` always tests P95 even with P50 scoring. Cost and memory limits test their comparison medians. Rejection reasons are retained for every excluded candidate.

The six default Pareto dimensions all minimize their values. A dominates B if it is no worse in every compared dimension and strictly better in at least one. With missing evidence, V1 conservatively compares only candidates with identical sets of fully available dimensions. This prevents dominance cycles and prevents missing evidence from erasing a real tradeoff. Equal candidates both remain on the frontier.

For every scored dimension:

```text
goodness = (maximum - value) / (maximum - minimum)
goodness = 1 when all available values are equal
effective_weight = requested_weight / sum(weights for available metrics)
overall_score = sum(effective_weight * goodness)
```

Normalization bounds come from all constraint-valid, evidence-valid candidates. Only Pareto candidates can win; dominated candidates keep informational scores and remain visible in the full ranking using the same bounds. At least **two comparable candidates before Pareto filtering** are required. One Pareto survivor after comparing several candidates is a valid recommendation.

| Objective | Latency | Cost | Reliability | Cold start | State overhead | Memory |
|---|---:|---:|---:|---:|---:|---:|
| balanced | .35 | .25 | .20 | .08 | .07 | .05 |
| latency | .65 | .05 | .15 | .10 | .05 | 0 |
| cost | .20 | .65 | .10 | 0 | 0 | .05 |
| reliability | .25 | .10 | .55 | .10 | 0 | 0 |

Override a preset with `--weights latency=0.5,cost=0.3,reliability=0.2`. Weights must be finite, nonnegative and sum to exactly 1.0 in decimal notation. Ties break by higher score, lower failure rate, lower P95, lower available cost, then alphabetical provider ID.

## Confidence and explanations

Confidence is deterministic, not an LLM judgment or a statistical significance test:

- **HIGH:** winner has at least five successful runs, latency CV < .15, score margin ≥ .08, all positively weighted metrics complete, no critical diagnostics, and complete comparison coverage.
- **MEDIUM:** winner has at least three successful runs, latency CV < .30, margin ≥ .08, required major weighted metrics complete and no critical diagnostics, but HIGH's stronger criteria are not all met.
- **LOW:** other cases, including close scores, missing major metrics or critical diagnostics. Standalone single-run aggregation is flagged exploratory; the CLI/engine will not start a formal recommendation with fewer than three measured runs.

Margin is winner score minus the highest-ranked other candidate's score. Close scores are described as close in score, without claiming a definitive best platform. Percent differences against the runner-up, cheapest, fastest, and best candidates in families/clouds are computed in code. A zero baseline has no invented percentage.

Family selection comes from the winning measured candidate. Reports show best FaaS, best Session-MCP, best latency, best cost, best reliability and best balanced, plus the full Pareto frontier. Specialized winners obey the same hard constraints and evidence eligibility.

## Fairness and cleanup

Every candidate exports `deployment_shape`: target count, total configured CPU/memory, and each target's resources and node grouping. The TravelPlanner example has seven 1-CPU/1024-MB FaaS targets versus two 2-CPU/2048-MB session targets. These are distinct deployment designs.

**This recommendation compares the deployment shapes defined by the benchmark configuration; it does not claim that the runtime families received identical resource allocations.** V1 optimizes provider/family selection while resource sizing stays fixed. The workload profile includes state access ratios, state domains, DAG depth and a structural parallelism estimate; none changes the winner.

Every deployment attempt reaches cleanup in `finally`, including partially failed deployments. **Current deployer APIs do not expose owned-resource deletion.** V1 records `cleanup_status=unsupported` and warns that resources may remain. Use the saved `url-map.json` and configured target names for manual cleanup. `--keep-resources` explicitly skips the attempt and records `kept`. Cleanup failures record `failed` and do not discard benchmark evidence. Per-run state cleanup in replay still runs. Resource sizing search and forced cold starts are future work.

## Artifacts and automation

```bash
uv run atsuite-recommend \
  --config benchmarks/TravelPlanner/config/faas7_mcp2_min.json \
  --fx-config docs/recommender_fx.example.json \
  --output-dir results/recommendations --json > recommendation.json
```

Human output includes a ranking table, winner, confidence, specialized winners, quantitative explanations, configured shapes, exclusions and cleanup status. `--json` prints only JSON to stdout; Python and subprocess progress goes to stderr. Exit codes: **0** recommendation produced, **1** unavailable comparison (artifact still written), **2** invalid arguments/configuration inputs.

By default, `results/recommendations/<benchmark>/<timestamp>.recommendation.json` contains schema version 1, experiment settings, objective and requested weights, FX rates, constraints, winner, rankings, specialized winners, normalization bounds, explanations, workload profile, all candidate statuses, complete statistics, metric availability, effective weights and run records. A sibling experiment directory contains config/trace snapshots and hashes, isolated run reports/events/evidence, per-candidate endpoint maps and incremental run records.

All scores can be recomputed offline from the saved run summaries: aggregate successful measurements, apply the recorded FX rates, select the recorded latency statistic, check constraints, construct the Pareto frontier, normalize with the recorded bounds and use the recorded weights. The integration tests exercise this reconstruction.

```python
from pathlib import Path
from atsuite.recommender.engine import RecommendationEngine

result = RecommendationEngine().recommend(
    Path("benchmarks/TravelPlanner/config/faas7_mcp2_min.json"),
    providers=["aws_lambda", "aws_agentcore"],
    runs=5,
    objective="balanced",
)
print(result.winner.candidate.id if result.winner else "Recommendation unavailable")
```

Tests use fake experiment runners and mocked runtime/collector APIs, with no cloud deployment:

```bash
uv run --with pytest python -m pytest -q
```
