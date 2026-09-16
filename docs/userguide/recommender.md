# ATSuite 自动部署优化器

本功能从 benchmark 配置生成合法的 provider、MCP topology 和 target 级资源候选，保存实验任务与证据，并在严格证据条件下给出推荐。预测值和 Advisor 文本不会替代实测 winner。

## 快速开始

### 1. 创建实验规格

```json
{
  "schema_version": 1,
  "benchmark": {
    "config": "benchmarks/TravelPlanner/config/faas7_mcp2_min.json",
    "correctness_policy": "require_declared_oracle"
  },
  "providers": ["aws_lambda", "aws_agentcore", "gcp_faas", "gcp_mcp"],
  "regions": {"aws_lambda": "us-east-1", "aws_agentcore": "us-east-1", "gcp_faas": "us-central1", "gcp_mcp": "us-central1"},
  "objective": {
    "type": "min_cost_under_slo",
    "base_currency": "USD",
    "cost_scope": "declared_runtime_scope",
    "max_p95_workflow_ms": 2000,
    "min_success_rate": 0.99,
    "strict_evidence": true
  },
  "search": {
    "mode": "adaptive",
    "max_candidates": 24,
    "baseline_coverage": "all_eligible_provider_families",
    "topology_strategies": [
      "baseline", "all_in_one", "per_node", "state_domain",
      "affinity_cluster", "dependency_partition"
    ],
    "max_generated_topologies": 8,
    "resources_mode": "provider_legal_grid",
    "per_target": true
  },
  "budget": {
    "currency": "USD",
    "max_estimated_spend": 50,
    "max_wall_time_seconds": 7200,
    "max_llm_tokens": 50000,
    "include_cleanup_reserve": true
  },
  "authorization": "plan_only"
}
```

### 2. 零云写入规划

```bash
uv run atsuite-recommend plan \
  --spec experiment.json \
  --output recommendation_results/demo/plan.json
```

`plan` 会同时生成：

```text
recommendation_results/demo/
  plan.json
  candidates/<candidate_id>/generated_config.json
```

生成配置把原 benchmark 的 trace 和 node 路径固定到源目录，并写入候选 topology 与 target 级资源。相同 provider、topology、architecture 但不同 memory/CPU 档位可以复用同一镜像构建。

独立 worker 通过 handler factory 执行已批准任务，避免 Web 请求线程直接执行云操作：

```bash
uv run atsuite-worker \
  --store recommendation_results/jobs.sqlite \
  --experiment EXPERIMENT_ID \
  --handlers my_worker:create_handlers
```

`create_handlers(store, experiment_id)` 必须返回 `BUILDING`、`DEPLOYING`、`EXPLORING`、`CONFIRMING`、`ANALYZING` 和可选 `CLEANUP` handler。缺少阶段 handler 会使任务明确失败；worker 不会把空操作标记为完成。

### 3. 启动控制面与 Web UI

```bash
uv run atsuite-serve \
  --host 127.0.0.1 \
  --port 8000 \
  --store results/recommender/jobs.sqlite \
  --artifact-root recommendation_results
```

打开 `http://127.0.0.1:8000/`。页面包括：新建实验、方案预览、实验进度、比较结果、推荐详情、模型与数据源。

若设置 `ATSUITE_API_TOKEN`，请求需发送 `Authorization: Bearer ...`。租户范围由以下请求头确定：

```text
X-ATSuite-Tenant
X-ATSuite-Project
X-ATSuite-Role: viewer | editor | admin
```

生产部署应由可信认证网关写入这些头，不应接受互联网客户端自行声明角色。浏览器修改请求会校验 `Origin`；跨域来源通过 `ATSUITE_ALLOWED_ORIGINS` 以逗号分隔配置。

## Benchmark 上传

`POST /api/v1/benchmarks` 接受原始 ZIP 请求体，文件名放在 `X-Filename`。控制面只解压和检查，不 import 上传代码。

上传会拒绝：

- 绝对路径与 `..` 路径穿越
- symlink、设备、FIFO、socket 等特殊条目
- 加密 ZIP
- 超过 25 MiB 的压缩包
- 超过 100 MiB 的解压后内容
- 超过 2,000 个条目
- 单文件压缩比超过 200

工具代码必须在隔离 worker 中执行。

## 推荐语义

确定性决策返回以下状态之一：

- `recommended`：完整 measured evidence 下有明确 winner。
- `no_clear_winner`：领先候选在预先声明的实际差异范围内。
- `no_feasible_candidate`：具有完整实测证据的候选全部违反硬约束。
- `insufficient_evidence`：必需指标缺失、过期、预测或没有 evidence ref。

固定锚点评分不会因加入一个更差候选而改变原候选得分。不同场景不会混排。`promising_unverified` 只用于后续选点。

## Predictor

```bash
uv run atsuite-predictor train \
  --dataset dataset.jsonl \
  --output models/model-20260916.json

uv run atsuite-predictor evaluate \
  --model models/model-20260916.json \
  --dataset holdout.jsonl \
  --output evaluation.json
```

训练器使用依赖最少的 ridge linear model。数据集支持 workload 分组与时间 cutoff 拆分。超出训练特征范围的预测标记 `ood=true`。无模型或无训练数据时使用明确的 baseline/missing 状态。

模型代码可运行不代表模型质量已经达标。启用 ML acquisition 前应保存未见 workload 的误差、候选召回和 regret 评估。

## Advisor

设置：

```bash
export OPENAI_API_KEY=...
export ATSUITE_ADVISOR_MODEL=<已批准的模型 ID>
```

Advisor 使用 Responses API Structured Outputs，仅发送白名单化的候选、指标、winner 和意图字段，并设置 `store=false`。它会记录 model、response ID、prompt template hash、catalog hash 和 token usage。

以下情况自动使用确定性模板：

- 未配置 `ATSUITE_ADVISOR_MODEL`
- SDK 或 API 不可用
- 模型拒答或没有结构化结果
- schema 解析失败

Advisor 返回后仍会校验 candidate ID、eligibility、metric ref，以及它是否试图覆盖确定性 winner。

## 市场数据

已提供：

- AWS Price List Query `GetProducts` 分页适配器
- GCP Cloud Billing Catalog `services.skus.list` 分页适配器
- Alibaba BSS `GetPayAsYouGoPrice` 适配器
- ECB 日参考汇率
- Open Exchange Rates 可选适配器
- Decimal 阶梯计价、向上舍入、usage 去重和币种交叉转换
- 不可变 JSON snapshot store

在线 API 找不到 SKU 时应返回 coverage 缺失。静态或手工数据必须保留来源和 freshness，不能标成实时价格。

## Replay 输出与正确性

每次 replay 会写入独立的 `outputs.json` 和 `correctness.json`。业务输出按 call ID 保存；`request_id`、`provider_request_id` 和 `timing` 等 provider 元数据不会参与默认业务结果比较。调用方可向 `run_trace()` 传入 `expected_outputs`，并为非确定结果提供 `output_comparator`。未声明 oracle 时 correctness 状态为 `not_configured`，不会伪装成已验证。

取消、失败和正常完成都会先进入 `CLEANUP_PENDING`。cleanup handler 失败会保存 `cleanup_error`；缺失实际阶段 handler 的任务会失败，不会被空操作标记为完成。

## 重新计价

`atsuite-recommend reprice` 读取实验的不可变 `usage_ledger.jsonl`、新的 rate-card snapshot 和可选 FX snapshot，重新执行 Decimal 阶梯计价。结果状态明确区分 `reprice_completed`、`reprice_missing_coverage`、`reprice_stale` 和 `insufficient_usage_ledger`，并保留原实验、usage ledger 与市场快照 digest。控制面将每次结果保存成新的 artifact。

## 工件与部署

`BuildArtifact` 包含 source tree、Dockerfile、dependency lock、platform 和镜像 digest。源码摘要递归覆盖构建上下文；provider、topology 和 architecture 进入 build identity。

向 `deploy()` 传入 artifact 时，部署器只接受 `repository@sha256:...`。若 provider 返回 effective resources 且与 requested resources 不同，部署立即失败。无法回读时记录 `configuration_readback=unverified`。

旧 CLI 不传 artifact 时保留原 tag/push 路径，以兼容已有工作流。自动推荐的 verified 部署链应使用已发布且可拉取的 registry digest。

## API 摘要

```text
POST /api/v1/benchmarks
GET  /api/v1/providers/capabilities
POST /api/v1/experiments/plan
POST /api/v1/experiments
GET  /api/v1/experiments/{id}
GET  /api/v1/experiments/{id}/events
POST /api/v1/experiments/{id}/cancel
POST /api/v1/experiments/{id}/resume
GET  /api/v1/experiments/{id}/comparison
GET  /api/v1/experiments/{id}/recommendation
POST /api/v1/experiments/{id}/reprice
POST /api/v1/experiments/{id}/advisor
GET  /api/v1/artifacts/{artifact_id}
POST /api/v1/deployments/plan
POST /api/v1/deployments/apply
```

创建实验支持 `Idempotency-Key`。相同 key 与相同请求返回原任务；相同 key 与不同请求返回 HTTP 409。SSE 支持 `Last-Event-ID`。

`deployments/apply` 只接受 admin、`confirm=true` 且实验明确使用 `production_apply` 授权。Web 请求线程只入队；外部 worker 负责变更、readback、健康检查和 rollback 证据。
