# 本地 ATSuite 接入 DeepSeek 官方 API

本教程用于本地运行 ATSuite、远程调用 DeepSeek 官方 API。API 模型 ID 为
`deepseek-flash`，对应 DeepSeek-V4.1-Flash；官方地址为
`https://api.deepseek.com`。核对日期：2026-10-09。
见 [官方模型与 API 信息](https://api-docs.deepseek.com/quick_start/pricing/)。

接入的是推荐优化器的 Advisor：解释证据和提出受审批边界约束的探索配置。
trace 中 `llm` 节点仍按记录时长回放；本配置不会将所有 benchmark 的 LLM 节点改为真实推理。
实际云部署仍需相应云凭证、镜像 registry 和 Docker。

## 1. 配置本地进程

在 [DeepSeek 控制台](https://platform.deepseek.com/) 创建 API Key，确认账号可调用目标模型。
推荐使用仓库根目录的 `.env`，以后新开终端也无需重新 export。
首次配置时，在仓库目录运行：

```zsh
uv sync --locked --group dev
cp -n .env.example .env
chmod 600 .env
```

用本地编辑器打开 `.env`，填写：

```dotenv
DEEPSEEK_API_KEY=你的真实APIKey
ATSUITE_ADVISOR_MODEL=deepseek-flash
```

CLI、Web、worker 和连通性检查统一自动加载仓库根目录的 `.env`，不依赖当前工作目录，
也不搜索上传的 benchmark 目录。`.env` 已被 Git 忽略，`.env.example` 只含空 Key 和模型名。
不要把 Key 发到聊天、写进规格 JSON 或提交进 Git。
加载遵循 [python-dotenv 的环境优先规则](https://bbc2.github.io/python-dotenv/)：
进程中已有的环境变量（包括空值）优先于文件。之前 export 过旧 Key/模型时，运行
`unset DEEPSEEK_API_KEY ATSUITE_ADVISOR_MODEL`，让新启动进程读取文件。
修改 `.env` 后重启 Web/worker；运行中的进程不会自动热更新凭证。

安装到其他位置或使用独立配置文件时，可在启动进程前设置
`ATSUITE_ENV_FILE=/绝对路径/atsuite.env`；这会替代默认文件路径。
`PYTHON_DOTENV_DISABLED=1` 可以关闭文件加载，仅使用进程环境。
仍可选择在终端直接 export，作为临时覆盖配置。
DeepSeek 使用独立的 `DEEPSEEK_API_KEY`，不借用 `OPENAI_API_KEY`，
也不采用 `OPENAI_BASE_URL`。未设置模型环境变量时，DeepSeek 默认选择 `deepseek-flash`。

## 2. 先检查，再做一次真实调用

```zsh
uv run --locked --group dev python -m tools.advisor_check
uv run --locked --group dev python -m tools.advisor_check --live
```

第一条只检查配置，不调用网络。第二条发送一次可能计费的结构化请求，默认输出上限为
512 tokens、整体 token 预留上限为 8192，不自动重试。可用 `--max-llm-tokens 20000`
或 `--max-output-tokens 1024` 显式调整上限。

| 状态 | 含义 | 退出码 |
| --- | --- | --- |
| `configuration_ready` | 环境变量就绪，还未调用 API | 0 |
| `blocked_external` | 缺少 API Key 或必需的模型配置 | 2 |
| `real_api_verified` | 实际返回通过结构与证据检查，含 response ID 和实际 token 用量 | 0 |
| `connection_failed` | 调用、预算、解析、证据校验或用量验证失败 | 1 |

真实调用输出包含 `real_api_verified: true`、`fallback: false`、`response_id` 与
`usage.input_tokens/output_tokens/total_tokens`。模板降级不计作接入成功，缺失用量不补零。
检查请求没有 benchmark 测量数据，不会创造云验证结果或推荐 winner；不发送工具源码、原始日志和凭证。
token 预留包含输入及结构化 schema 的保守字节上界，可能高于实际用量。
它限制派发 token，不能代替供应商账单或货币预算；本接入不猜测美元费用。

## 3. CLI 与默认 worker

在尚未冻结的实验规格中设置以下字段，其他 benchmark、云服务、地区、目标和预算仍按
[推荐器教程](recommender.md) 配置：

```json
{
  "advisor": {
    "enabled": true,
    "provider": "deepseek",
    "model_from_env": "ATSUITE_ADVISOR_MODEL",
    "send_source_code": false,
    "validate_evidence": true
  },
  "budget": {
    "currency": "USD",
    "max_estimated_spend": 50,
    "max_wall_time_seconds": 7200,
    "max_llm_tokens": 50000,
    "include_cleanup_reserve": true
  }
}
```

这是规格字段片段；`max_llm_tokens` 应覆盖本次实验的探索与最终解释调用。
超出上限会在发送 API 请求前拒绝并降级。实际环境变量决定模型；provider 随规格冻结。
已有 OpenAI 计划保持不可变，应使用新的计划输出路径重新规划并审批。

```zsh
uv run atsuite-recommend plan --spec experiment.json --output recommendation_results/deepseek/plan.json
uv run atsuite-recommend run --plan recommendation_results/deepseek/plan.json \
  --store results/recommender/jobs.sqlite --approve-budget --account aws_lambda=ACCOUNT_ID
uv run atsuite-worker --store results/recommender/jobs.sqlite --poll
```

替换账号并按规格添加其他 provider 的账号绑定。云执行可能计费，应使用相应审批预算。
CLI 的 `--wait` 和默认 worker 使用同一执行链，不需要编写 `my_worker.py`。
成功解释的实验记录包含 `explanation.provider: deepseek`、
`explanation.audit.fallback: false`、response ID 和实际用量；探索调用记在 `search_audit`。
调用失败保留降级原因，实验是否合格仍由统一证据服务决定。

## 4. Web

配置好 `.env` 后，从任意新终端启动服务器和 worker：

```zsh
uv run atsuite-serve --host 127.0.0.1 --port 8000 \
  --store results/recommender/jobs.sqlite --artifact-root recommendation_results
```

打开 `http://127.0.0.1:8000`，新建实验时将 **Advisor API 服务** 选择为
**DeepSeek（V4.1 Flash）**，启用 Advisor。Web 当前冻结 50000 tokens 的 LLM 预算；
在计划预览中检查预算，需自定义时使用 CLI 规格。
随后预览计划、冻结、审批并运行；worker 使用上一节同一个 store。
Key 只在服务器/worker 环境里配置，浏览器没有 Key 输入框，也不能指定任意 API 地址。
启用的 Web Advisor 需要绑定有效审批；关闭 Advisor 后，即使存在 Key 和模型变量也不会调用 API。

## 协议、验证与排障

使用 [官方 Responses API](https://api-docs.deepseek.com/guides/responses_api/) 与严格 JSON schema，
通过现有 SDK 调用。按 [官方思考模式说明](https://api-docs.deepseek.com/guides/thinking_mode/)
设置 `reasoning.effort=none`，限制单次调用的推理开销；每次请求最长等待 20 秒。
CLI、Web 和 worker 使用同一个 Advisor factory、预算与证据校验。
离线 compare 继续使用相同的确定性决策服务，无需 Key；LLM 不能改写 winner。

- `missing_deepseek_api_key`：确认 Key 已 export 到启动进程的终端，再重启进程。
- `AuthenticationError` / `PermissionDeniedError`：检查控制台 Key、账号权限及可用模型。
- `APIStatusError`：检查控制台余额、模型 ID 和服务状态；审计不输出可能含敏感信息的原始错误正文。
- `APITimeoutError` / `APIConnectionError`：检查本机网络和代理。没有自动重试，可手动再次执行检查。
- `RuntimeError`：检查 LLM token 预算，schema 和输入也占预留上界。
- `ValueError` / `evidence_mismatch`：输出被截断、拒绝或引用无效证据；降级结果不计作真实接入验收。
- `connection_failed` 且没有错误类型：检查 response ID 和实际 usage 是否完整，缺失用量不能验收。

本次实现的 HTTP 契约测试与浏览器 E2E 使用明确标记的模拟外部传输。
当前工作环境没有真实 DeepSeek Key，真实 API 验收为 `blocked_external`。
用户在本机执行 `--live` 得到通过结果后，才能确认该账号的真实模型接入；这仍不代表六种云服务已完成验收。
