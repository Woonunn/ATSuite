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
