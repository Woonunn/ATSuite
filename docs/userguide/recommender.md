# ATSuite 自动部署优化器

CLI、Web、默认 worker 和离线 compare 共用 `recommender.service` 与版本化证据门控。
默认 handlers 已随仓库提供；基本实验不需要编写 Python worker。
本地契约和浏览器测试使用明确的 simulated 外部边界，不能证明云部署成功。
最新逐项验收见 [修复状态](../developer-guide/recommender/remediation-status.md)。

## 安装和配置

三平台 `.env` 字段、SDK/CLI 登录、运行角色、registry 与状态 bucket 的配置见
[AWS、阿里云与 Google Cloud 配置教程](cloud-env.md)，模板见仓库根目录 `.env.example`。

```bash
uv sync --locked --group dev
```

实际执行需要 Docker/buildx、目标云凭证、已授权的执行角色、可推送的 registry 和明确地区。
GCP 需要 gcloud 和 `GOOGLE_CLOUD_PROJECT`；Alibaba 默认执行身份检查需要 aliyun CLI。
AWS 使用 SDK 默认凭证链。云凭证仅供部署/观测 worker 使用，不注入上传的构建代码。
`init.sh` 在无网络、限 CPU/内存/PID 的容器执行；设置 `ATSUITE_INIT_IMAGE=repository@sha256:<完整摘要>`。
运行角色访问预先建立的状态存储；`ATSUITE_STATE_BUCKETS` 是 provider 到 bucket 的 JSON 映射。
默认执行不创建或删除共享 bucket，不修改已有生产资源。

registry 默认为 AWS ECR、GCP Artifact Registry、Alibaba 的 `DOCKERHUB_NAME`。
可使用 `ATSUITE_REGISTRY_REPOSITORIES` 的 provider→repository JSON 映射，运行前配置 registry 登录与推送权限。
构建产物必须完成 registry push、远端 manifest 校验及按 digest 拉取检查；本地 Image ID 不充当云镜像摘要。

## 规划、审批和执行

benchmark 必须声明节点状态语义和确定性业务 oracle。配置示意：

```json
{
  "correctness": {"oracle": {"type": "exact", "version": "example-v1", "expected_output": {"answer": 42}}},
  "nodes": [{"name": "tool", "dir": "./nodes/tool", "trace_names": ["tool.run"], "state": {"mode": "stateless"}}]
}
```

有状态 workload 还需实际可读回的 `expected_state` 与 `state_output_node`。
没有 oracle 时可以规划，执行预检会明确拒绝 business verified。

实验规格仍为 schema 1（与 Analyzer schema 2、推荐 evidence schema 2 分开）：

```json
{
  "schema_version": 1,
  "benchmark": {"config": "benchmarks/YourBenchmark/config/benchmark.json"},
  "providers": ["aws_lambda", "aws_agentcore"],
  "regions": {"aws_lambda": "us-east-1", "aws_agentcore": "us-east-1"},
  "objective": {"type": "min_cost_under_slo", "base_currency": "USD", "max_p95_workflow_ms": 2000, "min_success_rate": 0.99},
  "search": {"max_candidates": 8},
  "protocol": {"warmup_runs": 2, "exploration_runs": 5, "confirmation_runs": 500},
  "budget": {"currency": "USD", "max_estimated_spend": 50, "max_wall_time_seconds": 7200, "max_llm_tokens": 5000, "include_cleanup_reserve": true},
  "authorization": "plan_only"
}
```

```bash
uv run atsuite-recommend plan --spec experiment.json --output recommendation_results/demo/plan.json
uv run atsuite-recommend run --plan recommendation_results/demo/plan.json \
  --store results/recommender/jobs.sqlite --approve-budget \
  --account aws_lambda=ACCOUNT_ID --account aws_agentcore=ACCOUNT_ID
uv run atsuite-worker --store results/recommender/jobs.sqlite --poll
```

`run` 默认返回 `execution_queued` 和 experiment ID。加 `--wait` 在当前 CLI 进程执行同一持久任务。
`authorization=approved_execution` 只是意图字段；有效审批另行绑定 plan digest、版本、操作者、账号、地区、预算与搜索边界。
plan 保留原始 baseline 和完整冻结 bundle，候选路径为 `candidates/<id>/bundle/config/generated_config.json`。
修改任意已冻结文件或注入新文件会使预检失败。计划不可原地改写。

在线价格按实际服务自动查询，找不到的 SKU/用量显示 coverage gap。
无法可靠估算构建、部署、调用和清理开销时停止派发，不按零元处理。
可在规划时使用已批准的市场快照：

```bash
uv run atsuite-recommend plan --spec experiment.json \
  --market-snapshot market.json --output recommendation_results/snapshot/plan.json
```

快照包含 rate cards、FX、来源/有效日期、费用范围，以及 `operation_cost_bounds` 的 build/deploy/invoke/cleanup 上界；
调用估算可用 `maximum_tool_calls`、`maximum_session_idle_seconds` 与正式费率推导。
也可设置服务器管理的 `ATSUITE_MARKET_SNAPSHOT`。快照随计划审批冻结，过期或缺项不伪装实时完整价格。
已批准的过期快照需在 spec 中显式设置 `market_data.stale_policy=allow`；仍保留过期状态，不能据此获得合格成本排名。
预算为保守派发限制，账单延迟和在途工作仍会形成 pending 费用。

## 状态、恢复和清理

成功路径为 PREFLIGHT → BUILDING → DEPLOYING → EXPLORING → CONFIRMING → ANALYZING → CLEANUP_PENDING → COMPLETED。
构建与部署有独立并行度；确认窗口没有构建任务竞争 runner。
阶段及子任务落库，恢复跳过已成功工作；未决远端调用需先对账，不能重复业务调用补日志。

```bash
uv run atsuite-recommend resume --experiment EXPERIMENT_ID --store results/recommender/jobs.sqlite
uv run atsuite-worker --experiment EXPERIMENT_ID --store results/recommender/jobs.sqlite
uv run atsuite-worker --reconcile-ttl --store results/recommender/jobs.sqlite
```

清理只针对账号、地区和 ownership 完整匹配的实验资源，保存删除回读。
清理失败保留推荐结果并把 job 标为 FAILED；恢复只重试清理。
云端异步删除保留 CLEANUP_PENDING，`--poll` 会继续检查删除结果直到清理完成。
取消不等同于云端停止计费。CANCELLED 不能直接恢复新的付费实验。
自定义 `--handlers` 仅为本地管理员扩展，需 `--allow-custom-handlers`；Web 不接收模块名。

## 比较与重新计价

```bash
uv run atsuite-recommend compare --manifest comparison.json --output offline-comparison.json
uv run atsuite-recommend reprice --experiment comparison.json \
  --market-snapshot new-market.json --output comparison-repriced.json
```

compare 可直接读取导出的报告或报告路径数组。旧报告可读，但缺版本化身份、确认试验、oracle、readback 和账本时标记 legacy_unverified。
重新计价使用不可变用量账本产生新报告与 parent digest，不修改原实验。
每正确成功 workflow 成本的分子保留全部正式尝试（包括失败/超时）的归因费用，零成功则 unavailable。

`recommended` 仅表示声明的 scenario、候选覆盖、业务约束和费用范围内可区分的已确认候选；不代表全局最优。
`no_clear_winner` 显示 Pareto 和权衡；`no_feasible_candidate` 需要全范围足够证据；缺样本、引用或覆盖返回 `insufficient_evidence`。
少量零失败不能证明高成功率。P95 使用独立 block 的分位区间，同时 deadline 完成比例包含失败/超时。
纯延迟目标未声明成功率时，计划冻结默认 `min_success_rate=0.95`；balanced 排序也计入失败率的不确定性。
模拟、预测和人工输入不会自动成为 `best_verified`。

## Web、身份与生产变更

```bash
uv run atsuite-serve --host 127.0.0.1 --port 8000 \
  --store results/recommender/jobs.sqlite --artifact-root recommendation_results
```

浏览器提供上传、新建、计划审批、进度、比较、推荐详情、数据源/模型、导出和重算。
上传只接收 ZIP，所有嵌套路径限于 bundle；控制面不导入上传工具。
断线后按事件游标恢复轮询。未启动 worker 的任务显示 queued，不显示完成。

开发模式仅 loopback。`ATSUITE_IDENTITY_TOKENS` 是服务器配置的 token→principal JSON，principal 含 subject、tenant、project、role、audience、expires_at。
客户端 `X-ATSuite-Role/Tenant/Project` 不决定权限。浏览器 token 存在 sessionStorage。
生产启动需要已配置身份、真实隔离 builder 与 ownership cleanup 能力，并使用受支持的生产存储。

生产 apply 使用独立目标 allowlist、配置快照、额外一次性审批、漂移/readback/健康/rollback 检查；模拟报告不能 apply。
AWS Lambda 目标适配器可配置 `ATSUITE_PRODUCTION_TARGETS`，其他生产目标未配置适配器时明确不可执行。

## Predictor 和 Advisor

`atsuite-predictor train/evaluate` 使用版本化 ridge baseline、完整 provider/region/mode/架构/逐 target 特征及 family/time 隔离。
`ATSUITE_PREDICTOR_MODEL_ID` 和 `ATSUITE_MODEL_REGISTRY` 指定模型；真实 holdout 质量不足或 OOD 时实际走 grid fallback。
ML 只影响探索选点，最终使用新的确认试验。没有真实 unseen-workload 评估时质量为 insufficient_real_data。

`plan --request 'P95 小于 2 秒且成功率至少 99%'` 可解析意图；未解析约束会阻止审批。
Advisor 支持 OpenAI 和 DeepSeek 官方 API。规格的 `advisor.provider` 选择服务，
`ATSUITE_ADVISOR_MODEL` 选择模型，Key 分别使用 `OPENAI_API_KEY` / `DEEPSEEK_API_KEY`。
CLI、Web、worker 和连通性检查自动读取仓库根目录 `.env`，已有环境变量优先；
可复制 `.env.example` 后填写本地 Key，修改后重启相应进程。
完整配置与真实连通性检查见 [本地接入 DeepSeek-V4.1-Flash](deepseek.md)。
预算限制 token；调用失败使用确定性模板，关闭 Advisor 时不发起模型调用。
结构化解释只能引用实际候选和有单位/范围的证据，不得改变确定性 winner。
真实模型 API 及云验收状态独立记录，未执行时均为 blocked_external。
