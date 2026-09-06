# 自动部署策略推荐

ATSuite Recommender 根据重复 benchmark 的实测证据，选择 **云厂商 + 运行时家族 + benchmark 已配置的部署拓扑**。它不使用静态规则预测最佳 provider；`stateless/r/w/rw` 等状态语义仅用于描述和解释工作负载，不参与评分。

[English documentation](recommender.md)

## 快速开始

先安装需要的 provider 依赖，并按已有 build/deploy/replay 流程配置凭据、镜像仓库、存储和可观测性，启动 Docker：

```bash
uv sync --group aliyun --group aws --group gcp
uv run atsuite-recommend \
  --config benchmarks/TravelPlanner/config/faas7_mcp2_min.json \
  --runs 5 \
  --objective balanced \
  --fx-config docs/recommender_fx.example.json
```

示例中的 `CNY: 0.14` 是用于展示格式的固定输入，不代表实时汇率；正式比较时请使用自己选定的汇率。Provider 配置指南：[Alibaba](userguide/ali.md)、[AWS](userguide/aws.md)、[GCP](userguide/gcp.md)。

命令会真实构建镜像、部署目标、调用云服务，因此需要相应权限并产生正常云费用。它沿用配置的部署名称，可能更新同名部署。V1 不修改 CPU、内存、目标数量或分组。

## 候选与执行流程

| 云厂商 | FaaS | Session-MCP |
|---|---|---|
| Alibaba | `ali_fc` | `ali_agentrun` |
| AWS | `aws_lambda` | `aws_agentcore` |
| GCP | `gcp_faas` | `gcp_mcp` |

候选直接复用 `atsuite.pipeline.PROVIDERS`，逐一调用 `resolve_benchmark()` 验证能力。只有 FaaS pipeline 的配置会保留 Session-MCP 候选及 unsupported 原因。现有 `pipeline.session` 和 `pipeline.mcp_serverless` 别名由 resolver 统一处理。单个 provider 不支持、构建失败、部署失败或运行失败，均不会中止其他候选。

默认排除 `mcp_gateway`；可通过 `--include-provider mcp_gateway` 或 `--providers` 显式加入。其现有 pricing policy 尚未提供可比较成本，成本保持 unavailable。

```text
validate → build → deploy → warmup → measured runs
         → AnalysisReport → 聚合 → 尝试清理
```

执行器直接调用 Python API：`build_resolved_targets`、`deploy`、`run_trace` 和 `Analyzer`，不通过 subprocess 调 ATSuite CLI。已有 API 内部调用的 Docker/provider 工具仍正常工作。

默认预热 **1 次**、正式测量 **5 次**；`--runs` 最小为 **3**，`--warmup-runs` 可以为 0。每次预热和正式运行使用独立的 experiment/provider/run UID。预热不进入评分统计和失败率。候选和重复运行按记录的顺序串行执行，保留 trace 延迟及模拟 LLM 时间。结果主要体现稳定运行及自然出现的冷启动，V1 不强制制造冷启动。沿用 Analyzer 的日志等待策略，session usage 日志可能需要较长等待时间。

## 指标和统计方法

推荐器读取 `AnalysisReport.summary`，沿用现有 collectors 和 pricing policies，不再访问云 API 计算第二套指标或定价。

| 评分指标 | 原始字段/证据 | 用于比较的统计量 |
|---|---|---|
| `latency` | `run_user_e2e_ms` | 默认 P95，可选 P50、median |
| `cost` | `total_price` + `currency` | 成功 workflow 的 USD 成本中位数 |
| `reliability` | 正式运行结果 | 失败正式运行数 / 全部正式运行数 |
| `cold_start` | `cold_start_total`、`invocation_count` | 成功运行的冷启动总数 / invocation 总数 |
| `memory` | `avg_memory_usage_mb` | 各成功 workflow 实测平均内存的中位数 |
| `state_overhead` | `total_state_sync_overhead_ms` | 成功 workflow 状态同步开销中位数 |

延迟、成本、内存、冷启动次数、状态同步、platform 和 network 指标均保留 samples、count、mean、median、min、max、P50、P95、stddev 和 CV。Compute、initialize、framework overhead、idle ratio 也输出，但默认不进入评分。

P95 固定采用 **type-7 线性插值**：对样本排序，计算 `h=(n-1)*0.95`，在 `floor(h)` 和 `ceil(h)` 对应值之间插值。P50 等于 median。标准差使用样本标准差（分母 `n-1`）；单个样本时为 0。`CV=stddev/mean`，全零样本的 CV 为 0。空统计使用 JSON `null`。不删除任何 outlier。

连续性能和成本统计只使用成功的正式运行。失败和预热的原始记录全部保留，因此快速失败不会降低延迟统计。这里的成本**不是完整云账单**：失败运行、预热、构建、遗留资源以及 Analyzer 计费窗口外的空闲费用，都可能带来额外支出。

部署失败单独记录状态，不虚构为五次运行失败。超时、runtime/provider error 和工具返回 `isError` 都计为 workflow 失败。采集失败与清理错误单独记录，不会仅因这些错误把已完成的 workflow 计为失败；它们会影响指标完整性和置信度。报告额外输出 invocation failure rate。正式候选需要至少 `max(2, ceil(runs*0.6))` 次成功，并记录全部请求的正式运行结果。

## 指标可用性与汇率

指标具有 `available`、`partial`、`unavailable` 状态。Analyzer 保留原有数值字段以兼容 replay，同时增加实际采集覆盖状态：配置内存不等于实测内存，默认冷启动数 0 不等于观测到 warm run，客户端耗时代替 billing duration 不构成完整成本证据。

部分数据保留在原始统计中，但**不参与评分**。评分指标需要覆盖成功运行及相关 invocation；旧报告缺少 availability 元数据时，仅可使用已记录的 workflow latency。未知值绝不自动当作 0。

```json
{
  "base_currency": "USD",
  "rates": {"USD": 1.0, "CNY": 0.14}
}
```

汇率表示每单位原币对应多少 USD，`USD` 固定为 1。不会在线拉汇率。没有 CNY→USD 时，Alibaba 成本 unavailable。报告保留 `pricing.normalized_currency` 和精确的 `pricing.fx_rates`。

Latency 和正式运行 reliability 始终必需。非 balanced objective 还要求其同名指标完整；显式 `--weights` 中所有正权重指标也必需。硬约束要求对应指标完整，否则淘汰候选。可选指标缺失或 partial 时，从该候选分母移除并重新归一化权重，同时记录有效权重、解释和置信度惩罚。

## 硬约束、Pareto 与评分

推荐顺序固定为：**硬约束过滤 → Pareto 分析/过滤 → 确定性加权评分**。违反约束的候选不会被加权平均救回。

```bash
uv run atsuite-recommend \
  --config benchmarks/TravelPlanner/config/faas7_mcp2_min.json \
  --providers aws_lambda,aws_agentcore,gcp_faas,gcp_mcp \
  --cloud aws,gcp --family faas,session \
  --max-p95-ms 2000 --max-cost-usd 0.01 \
  --max-failure-rate 0.01 --max-memory-mb 1024 \
  --objective cost
```

Cloud/family 和数值硬约束在测量后统一过滤；如果要限制实际部署的平台，使用 `--providers`。即使 `--latency-stat p50`，`--max-p95-ms` 仍检查 P95。成本和内存上限检查各自的比较中位数。报告保留每项淘汰原因。

六个默认 Pareto 维度均为越小越好。A 在所有比较维度不差于 B，且至少一个维度更好，才支配 B。对于缺失数据，V1 保守地仅在“完全可用的维度集合相同”的候选之间判断支配，防止不同缺失集合形成支配环，或缺失证据消除真实 trade-off。相同表现的候选都保留。

```text
goodness = (max - value) / (max - min)
所有可用值相等时 goodness = 1
effective_weight = requested_weight / sum(可用指标的权重)
overall_score = sum(effective_weight * goodness)
```

Min/Max 来自所有满足证据要求和硬约束的候选。只有 Pareto 候选能胜出；被支配的候选仍用同一组边界计算信息性分数，显示在完整排名里。Pareto 过滤前至少需要 **两个可比较候选**；测量多个候选后只剩一个 Pareto 候选，可以正常推荐。

| Objective | Latency | Cost | Reliability | Cold start | State overhead | Memory |
|---|---:|---:|---:|---:|---:|---:|
| balanced | .35 | .25 | .20 | .08 | .07 | .05 |
| latency | .65 | .05 | .15 | .10 | .05 | 0 |
| cost | .20 | .65 | .10 | 0 | 0 | .05 |
| reliability | .25 | .10 | .55 | .10 | 0 | 0 |

可用 `--weights latency=0.5,cost=0.3,reliability=0.2` 覆盖 preset。权重必须是有限非负数，十进制表示之和严格等于 1。确定性 tie-break 顺序：更高总分、更低失败率、更低 P95、更低可用成本、provider ID 字母序。

## 置信度与解释

置信度由代码确定，不是 LLM 判断，也不等同于显著性检验：

- **HIGH**：winner 至少 5 次成功，latency CV < .15，分差 ≥ .08，所有正权重指标完整，无关键诊断问题，候选比较覆盖完整。
- **MEDIUM**：winner 至少 3 次成功，CV < .30，分差 ≥ .08，参与评分的主要指标完整，无关键问题，但尚未满足 HIGH 的更严格条件。
- **LOW**：其他情况，包括分数接近、缺失主要指标、关键诊断等。统计 API 可以汇总单个样本并标记 exploratory；CLI/engine 不会用少于 3 次正式运行启动推荐。

分差为 winner 分数减去最高分其他候选。分差小时明确说明分数接近，不声称绝对最佳。与 runner-up、cheapest、fastest、各 family/cloud 最佳候选之间的百分比均由代码计算；基线为零时不虚构百分比。

最终 family 直接来自获胜候选，无单独的“状态多就选 MCP”规则。报告同时输出 best FaaS、best Session-MCP、best latency、best cost、best reliability、best balanced 和 Pareto frontier。专项 winner 同样遵守硬约束和证据有效性要求。

## 公平性与清理

每个候选输出 `deployment_shape`：目标数、配置 CPU/内存总量、各目标资源和节点分组。例如 TravelPlanner 配置是 7 个 1 CPU / 1024 MB FaaS 目标，对比 2 个 2 CPU / 2048 MB session 目标，是不同的部署设计。

**推荐比较的是 benchmark 配置中定义的部署拓扑，不代表各运行时家族获得了相同的资源分配。** V1 优化 provider/family 选择，资源配置保持固定。工作负载 profile 包括状态访问比例、状态域、DAG 深度和结构性并行度估计，但不覆盖实测排名。

任何部署尝试都会进入 `finally` 清理路径，包括部分部署失败。**当前 deployer API 未提供已拥有资源的删除接口**：V1 输出 `cleanup_status=unsupported` 及资源可能遗留的 warning。可以结合保存的 `url-map.json` 和配置目标名称手动清理。`--keep-resources` 显式跳过尝试，记录 `kept`。清理异常记录 `failed`，不丢弃 benchmark 证据；replay 中原有的单次运行状态清理仍会执行。资源规格搜索、强制冷启动留待后续版本。

## 输出与自动化

```bash
uv run atsuite-recommend \
  --config benchmarks/TravelPlanner/config/faas7_mcp2_min.json \
  --fx-config docs/recommender_fx.example.json \
  --output-dir results/recommendations --json > recommendation.json
```

终端显示排名、winner、confidence、专项 winner、定量解释、部署拓扑、淘汰原因和清理状态。`--json` 的 stdout 只包含 JSON，Python 和子进程日志发送到 stderr。退出码：**0** 已生成推荐，**1** 无法比较（仍输出 artifact），**2** 参数或输入配置错误。

默认 artifact：`results/recommendations/<benchmark>/<timestamp>.recommendation.json`，schema version 为 1。包含实验配置、objective、权重、汇率、约束、winner、排名、专项 winner、归一化边界、解释、工作负载 profile，以及每个候选的状态、完整统计、可用性、有效权重和 run records。同名实验目录保留 config/trace 快照及哈希、独立的 report/events/evidence、候选 URL map 和增量运行记录。

可离线从原始 run summary 复算：汇总成功运行、应用记录的汇率和 latency-stat、检查硬约束、计算 Pareto、使用记录的 Min/Max 与权重评分。集成测试验证了此过程。

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

测试使用 fake experiment runner 和 mock runtime/collector，不部署云资源：

```bash
uv run --with pytest python -m pytest -q
```
