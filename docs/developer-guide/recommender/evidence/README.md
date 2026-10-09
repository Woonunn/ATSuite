# Executed remediation evidence

The final acceptance record is [../remediation-status.md](../remediation-status.md).
Logs are repository-local test output, not live cloud evidence.
Trailing line whitespace is normalized for repository formatting checks; test
names, failures, assertions, observations and verdicts are preserved.

- `locked-baseline.txt`: original de752f2 archive tested in the synchronized
  project environment. `baseline.txt` is the earlier incomplete environment
  failure and is deliberately retained, not reported as the final baseline.
- `*-before.txt`: failure-first reproductions, including original review defects,
  counts/source/constraints, build identity, budget/lease recovery, frozen-bundle
  injection, missing AgentCore usage, deleted endpoint recovery and asynchronous
  deletion. `cloud-entry-before.txt` reproduces the local acceptance classifier;
  it does not execute cloud services.
- `*-after.txt`, `*-tests.txt`, `planning-regressions.txt`: scoped verification at
  the documented stage. `working-suite.txt` and `migration-tests.txt` are
  intermediate integration output, not the final verdict.
- `final-browser-suite.txt`: successful earlier combined 236-test run with real
  Chromium. External cloud/registry interfaces were explicitly simulated.
- `reprice-browser-before.txt`: a later full run exposed the browser version-list
  failure; no assertion or confirmation sample was removed to resolve it.
- `final-suite.txt`: final combined locked all-groups run, 241 tests, 240 passed
  and 1 live PostgreSQL skip. All three real Chromium browser tests passed.
- `reprice-browser-after.txt`: additional targeted browser verification of the
  new version selection and unchanged original report digest.
- `cloud-acceptance-help.txt`: opt-in entry help only; not live-provider evidence.

Each FIX ID's source files, commands, local commits and remaining external
acceptance are listed in the remediation status document. No log here establishes
real cloud verification, production apply, real LLM compatibility or held-out
model quality. These limitations are recorded independently from local passes.
