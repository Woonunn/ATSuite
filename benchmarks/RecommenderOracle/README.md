# Deterministic output and state oracle

The counter workload adds 2, then 3, then returns its complete business state.
Every operation runs through the SDK state transaction. A trial starts with an
empty counter and must finish with `{ "total": 5, "amounts": [2, 3] }`.
`oracle/exact.json` checks every business output and the final state, rather than
assuming a successful HTTP response proves correctness.

`config/benchmark.json` defines both FaaS and session routes. Both use logical
state domain `counter`; each experiment/candidate/scenario/session has its own
`StateKey`. The original configuration stays in the plan's baselines even when
a provider rule excludes its resources (for example, Lambda derives CPU from
memory and does not accept the original independent CPU value).

The local contract test runs the real repository tool code and SDK transactions:

```sh
uv run python -m unittest tests.test_recommender_oracle_benchmark -v
```

This test demonstrates deterministic output/state equivalence and isolated UIDs.
It does not verify cloud deployment, billing, registry publication, or cloud
state persistence. Those require separately approved provider acceptance.

## CNY 5, single-round smoke comparison

From the repository root, this helper compares `aws_lambda`, `aws_agentcore`,
`ali_fc`, and `ali_agentrun` in `us-east-1`. `ali_agentrun` is the existing FC
Session MCP adapter, not native Alibaba AgentRun. Each provider gets one
candidate and one confirmation attempt, with no warmup, exploration, or
automatic measurement retries. It uses the real cloud backend and exact oracle.

```sh
uv sync --group aliyun
# Review a plan without deploying or invoking anything:
uv run python -m tools.recommender_budget_once \
  --output results/recommendations/budget_once_preview
# Start a NEW billable experiment; choose a fresh output directory each time:
uv run python -m tools.recommender_budget_once \
  --output results/recommendations/budget_once_rerun_01 --confirm-live
```

The helper loads `.env`; keep credentials there, outside Git. Configure
`AWS_ACCOUNT_ID`, `ALI_ACCOUNT_ID`, the existing execution roles/registries,
`DOCKERHUB_NAME`, and `ATSUITE_STATE_BUCKETS`. Both Alibaba entries must name
an existing bucket owned by the approved account in `us-east-1`, accessible
to the FC execution role. For the October 11 experiment these entries are
`atsuite`, and the Docker Hub repository is `woonunn/atsuite`. The helper
checks OSS existence, region, and ownership before building or deploying.
Docker must be running and authenticated to Docker Hub and the AWS ECR registry.

Official prices and FX are fetched afresh by default. An existing fresh official
snapshot may be passed with `--market-snapshot path/to/market.json`; stale or
incomplete prices are rejected. Dispatch reservations total CNY 4.48 against
a CNY 5 estimate cap. AgentCore sessions are capped at 90 seconds with a
60-second idle timeout; deployment readback verifies the limits. A price
increase that exceeds the reserve blocks execution before deployment.

`plan.json`, `jobs.sqlite`, `run.log`, and `result.json` live under `--output`;
`result.json.report_path` points to the formal comparison. A completed old job
cannot be remeasured with `resume`; the helper refuses a nonempty output
directory. It cleans owned experimental compute, but retains published images
and shared buckets. These retained resources can incur further charges; the
estimate assumes at most 24 hours of incremental registry retention and is
not a provider-side spending limit or an actual invoice.

One sample cannot establish a statistically verified winner. Provider billing
logs may arrive after cleanup, so the formal comparison can report
`insufficient_evidence`. The earlier supplemental cost comparison waited for
delayed logs separately; this helper uses the standard 60-second usage wait
and does not reproduce that manual billing recovery. Missing cost is not zero.
