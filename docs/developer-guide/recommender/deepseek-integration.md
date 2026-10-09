# DeepSeek official API integration — local validation

This follow-up starts from `bf120c59e6391273ba689f1bdf802d06aa3a6a2a` on
`feat/recommender`, with a clean worktree. No branch switch, reset, rebase,
stash or remote push was performed during this integration.
The baseline is the preceding remediation's 241-test combined run:
240 passed, one unconfigured live PostgreSQL test skipped.
See [remediation status](remediation-status.md).

## Reproduction and implementation

The frozen advisor provider was previously ignored by worker proposal generation,
final explanations and the Web API: all instantiated `OpenAIAdvisor` directly.
There was no official DeepSeek adapter, provider validation or connectivity tool.
An explicit empty model could inherit the global model variable, allowing a
disabled advisor path to dispatch an unintended request.

Before implementation, seven regression tests produced two failures and seven
subtest errors. The failures capture the disabled-model and unknown-provider
bugs; errors capture the missing adapter/factory.
After implementation, those same seven tests passed.
Evidence: [before](evidence/deepseek-before.txt),
[after](evidence/deepseek-after.txt).
Saved logs remove trailing whitespace only; outcomes and assertions are unchanged.

`DeepSeekAdvisor` uses the official `https://api.deepseek.com/responses` endpoint,
`DEEPSEEK_API_KEY`, default model `deepseek-flash`, strict JSON schema,
`reasoning.effort=none`, no automatic retries and the existing bounded request
timeout. The shared factory selects the frozen provider in each production
caller. Credentials and endpoints are not accepted from the browser or spec.
Disabled advisors cannot create an SDK client. Missing keys and exceeded token
budgets are rejected before dispatch.

The existing redacted catalog, proposal materializer, approval bounds and metric
verification are retained. Legal claims and the deterministic winner can pass;
fabricated metrics and a substituted winner fall back. Missing usage does not
become a zero-cost observation. Cloud evidence remains simulated in local tests.

`tools.advisor_check` checks configuration without network by default. `--live`
requests one bounded potentially billable call; success requires provider match,
valid structured evidence, no fallback, response ID and actual token usage.
The empty evidence catalog used for this check is a connectivity probe, never
a benchmark measurement or cloud acceptance report.

## Local contracts and browser coverage

`tests.test_deepseek_advisor` exercises the real installed SDK with explicitly
simulated HTTP transport. It covers official host/key isolation, schema and
usage, missing credentials, disabled advisor, request budget, timeout, HTTP
failure, incomplete response, invalid references, valid claims, winner override,
missing cost usage, configuration-only checking, frozen provider validation,
default worker proposal/explanation execution and the approved Web API.
The default worker test retains all ten warmup/pilot/confirmation attempts,
keeps the approved plan unchanged and cannot produce a cloud-verified winner.

The existing real Chromium workflow now selects DeepSeek in the form and asserts
that its provider and disabled flag persist in the frozen plan. Its existing
600 confirmation attempts, simulated evidence assertions and repricing checks
are preserved.

Full combined validation results are recorded in the follow-up section of
[remediation status](remediation-status.md).
User setup and the opt-in real API check are in
[the DeepSeek tutorial](../../userguide/deepseek.md).

## Follow-up: persistent local .env configuration

`atsuite.config.load_local_env` loads the repository-owned `.env` at startup for
the recommend/serve/worker CLIs, direct Web app factory and connectivity checker.
It uses the existing locked python-dotenv dependency and preserves every existing
process variable, including empty values. Default lookup is anchored to the
project, never the current workload directory. `ATSUITE_ENV_FILE` allows an
operator-selected file; `PYTHON_DOTENV_DISABLED=1` disables file loading.
There is no credential hot reload. `.env` and `.env.*` are ignored by Git;
the tracked `.env.example` contains only an empty Key and default model.

Eight failure-first regressions produced five failures and six subtest errors
before implementation. The combined affected-path validation then passed all
48 tests in 4.803 seconds, covering config loading and priority, missing files,
untrusted working directories, explicit files, disabling file loading, every
entrypoint, existing Advisor contracts, default execution, CLI and Web approval.
No network request was needed to configure credentials.
Evidence: [dotenv before](evidence/dotenv-before.txt),
[dotenv after](evidence/dotenv-after.txt).
The preceding 254-test browser-inclusive result remains the DeepSeek adapter
baseline; it is not presented as a rerun after this configuration follow-up.
Real model/cloud acceptance remains `blocked_external`.

The user's subsequent local configuration check reported `configuration_ready`,
`api_key_configured: true` and `real_api_verified: false`. This confirms that
local configuration is loaded; authenticated API acceptance still requires a
successful opt-in live check. No local credential file is included in this commit.

## External acceptance

**DeepSeek authenticated API acceptance: `blocked_external`** — this environment
has no `DEEPSEEK_API_KEY`, and no paid or authenticated model request ran.
`advisor_check` reports `real_api_verified: false`, the missing variable and
`blocked_external`, with exit code 2. Simulated SDK contracts cannot change this.
Six-provider cloud acceptance remains independently `blocked_external`.
This integration configures the recommender Advisor; trace LLM replay remains
the recorded-duration execution described in AGENTS.md.
