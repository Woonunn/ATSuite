# Provider Performance & Cost Model（PoC）

`atsuite.provider_model` 在 `main` 的 Workload Model、Analyzer、PricingPolicy 和 E1-A 记录上增加离线/在线统计模型。无新增运行依赖，无云端副作用，无 Router 或 Scaling Controller。`ali_agentrun` 的名称仅为现有 Adapter ID，模型显示名称为 **Aliyun FC Session MCP**。

## 接口与数据口径

```python
from pathlib import Path
from atsuite.provider_model import ProviderModel

model = ProviderModel.load(Path("results/provider-model/e1a-v3/model.json"))
# workload_prediction = workload_model.predict(arrival_ns)
# pools 为部署读回的配置，environment 对应冻结版本，不能将 requested 当作 actual。
prediction = model.predict(
    workload_prediction, pools, load_rps=0.3,
    horizon_s=600, as_of_ns=arrival_ns,
)
```

`predict` 接受单个 Workload Profile、Profile 列表，或 `WorkloadModel.predict()` 的输出。Workload Model 新增 `joint_profiles` 字段，保留特征联合分布；旧的只有边缘分布的输入退化为均值条件预测，返回 `workload_basis=marginal_means` 和低置信度。空列表不产生虚构样本。

每个 pool 的结构如下；数值仅说明接口，不是平台实测能力：

```python
pool = {
    "provider": "ali_agentrun",  # 四种 ID 见 BACKENDS
    "environment": "冻结数据集、Capture、Graph、Tool及依赖版本的标识",
    "resources": {"region": "us-east-1", "cpu": 1, "memory_mb": 2048},
    "instances": 2,               # 实际/待预测实例数；未知为 None
    "sessions_per_instance": 1,   # 独立 Session 槽位
    "invocations_per_instance": 200,  # 实例 RPC 并发，与 Session 槽位不同
}
```

Provider/Mode 是固定映射。不同 environment、资源配置、Session 或 invocation 并发限制的样本不混用；没有对应资源配置的训练样本就返回未知。实例数和负载参与条件匹配，超出训练范围标记外推。Lambda reserved concurrency 不是已存在的实例数，AgentCore requested CPU/memory 不是实际可用资源。

特征复用 `state_bytes=S_G`、`llm_idle_s=D_G`、`rho_kb_s=S_G/1000/D_G`、Tool CPU/参考耗时、状态读写量、最大状态对象、节点调用/因果重访/依赖边计数及并行峰值。参考耗时来自固定 Capture 或已完成请求，不是新请求的执行结果。Tool elapsed 仍可能带有参考环境的 RPC 开销，不能视为纯 CPU 需求。平台执行时间、同步、冷启动、调度、排队是独立输出；调度时间不冒充排队时间。MCP 常驻状态的 transport bytes=0 不意味着 S_G=0。

## 模型与不确定性

每个候选执行池分别选择默认 20 个近邻。非负特征使用 `log1p` 与训练范围归一化；缺失特征不补零，相同距离优先最近完成的观测。对每个输入联合样本选择相同数量的近邻，再合并原始结果计算经验分布。这是局部统计条件模型，既不是因果效应估计，也不是复杂机器学习模型。

输出包含原始样本、均值、p50、p95、覆盖率、独立样本数、缺失特征、外推特征与训练范围。p10/p90 是经验分位数，不是参数置信区间。重复命中同一观测不会提高 `selected_unique_count`；少于 20 个独立样本时标记低置信度。延迟分布只包含成功请求，同时返回失败/不完整请求计数，不能据成功分布推断超时请求表现。

冷启动和队列影响来自匹配负载/容量条件下的实测联合结果；不额外叠加它们以免重复计算。未知负载条件的尾延迟是低置信度近邻外推，不是排队论保证。不同后端 p95 始终独立；不加权平均得到“系统 p95”。

容量使用槽位守恒上界：

- decoupled：槽位数 = 实例数 × invocation 并发；slot-seconds 是一次 Agent 请求全部 Tool 占用的实例槽位时间，包含已知初始化时间。
- coupled：槽位数 = 实例数 × Session 并发；slot-seconds 必须包含 Session 驻留和释放尾部，不能用 Tool HTTP 时间或 Agent 完成时间代替。
- `max_service_rps = slots / mean(service_slot_s)`；`effective_rps=min(load,max_service_rps)`，到达容量上界时提示不足风险。这是理想工作守恒上界，不是 SLA；CPU争用、状态亲和与负载突发可能进一步降低容量。缺少实际槽位或占用证据时不输出容量数值。
- `observed_goodput_rps` 是另一个独立输出，来自完整观测窗口，不能冒充饱和容量。

## 在线更新与成本

使用 `model.complete(trace, pool, load_rps, reference_profile=...)` 在 Agent 请求结束时记录本地结果；`model.observe(profile, analyzer_report, pool, load_rps, available_ns)` 关联单请求 Analyzer 报告。若使用固定参考 Profile，延迟日志通过复制 `complete()` 返回的记录、补充 measurements/cost、更新 available_ns 后调用 `update()`。

`update()` 的归一化记录字段为：

```python
row = {
    "kind": "request",             # 或 window
    "id": "全局稳定请求ID或窗口ID",
    "pool": pool,
    "profile": reference_profile,  # 已知的固定参考特征或实际已完成 Profile
    "status": "success",           # failed / incomplete 仅保留审计
    "load_rps": 0.3,
    "arrival_ns": start_ns,
    "completed_ns": end_ns,
    "available_ns": evidence_available_ns,
    "measurements": {"latency_s": 12.3, "state_sync_s": None},
    "cost": None,
}
model.update(row)
model.save(Path("provider-model.json"))  # 原子写入 provider-model.json.gz
```

Provider/environment/kind/id 联合去重。稍后的证据可产生新 revision，保留先前历史；预测只使用严格早于 `as_of_ns` 的最近 revision。更改请求身份、到达/完成时间、负载或 Profile 的修订会拒绝。采集时间不能回填为请求完成时间。线程内安全；不支持多个进程同时写同一快照。

成本结构：

```python
row["cost"] = {
    "scope": "pool_total",
    "basis": "actual",          # 或 estimated
    "currency": "CNY",          # 不自动换汇
    "complete": True,
    "attribution": "exclusive",
    "evidence": ["可追溯、已核验且独占归属该窗口的账单证据"],
    "components": {"compute": 1.2, "retention": 0.4, "idle": 0.2, "state": 0.1},
}
```

分项必须互斥；`compute` 已包含的内存不能再计入 `retention/idle`。共享实例或 Session 费用按 `kind=window`、同一稳定窗口 ID 导入一次；不要复制到每个请求。窗口预测是包含全部分项的总额，按观测窗口费率缩放到 horizon（平稳假设），与 request 预测**不相加**。实际费用只能由已经核验独占归属、对应 scope 完整的证据导入；模型检查声明和数值，不能替代账单核验。账户日总额不具备请求/窗口独占归属。

预测统一标记 `basis=predicted`，并保留 `training_basis`。不完整成本只有 `subtotal`，`total=None`；没有成本观测时返回空列表和 `cost_missing=true`。成本分项保持调用方命名，互斥性由采集方保证。不同 scope、完整性、币种、actual/estimated 不混合。

E1-A 成本复用现有 PricingPolicy 的版本化费率，因此只作估算，不宣称当前区域、阶梯、折扣后的账单价格：

| 后端 | 导入的成本 scope | 不能从该分项推导的内容 |
|---|---|---|
| Lambda | lambda_invocations | S3、日志、网络、其他保留费用 |
| AgentCore | agentcore_session_usage | 未交付使用量、其他服务；共享 Session 不分摊到请求 |
| Ali FC | fc_active_invocations | warm 实例 idle/保留、OSS 等 |
| Ali FC Session MCP | fc_mcp_tool_requests | CPU、内存、idle、保留；HTTP 响应时长不代表异步 Tool 计算 |

Lambda Pricing 已修正重复加入 init duration 的问题：[AWS 说明 Billed Duration 包含可计费初始化](https://aws.amazon.com/blogs/compute/aws-lambda-standardizes-billing-for-init-phase/)。只有缺少 Billed Duration 的原有估算回退路径才单独加入 init。

LangGraph Capture/Replay CLI 增加 `--provider-model PATH --provider-context JSON`，context 为 `pool`、`load_rps` 和可选 `reference_profile`。可同时提供现有 `--workload-model`，请求开始前冻结预测，完成后自动观测，生成 `provider_prediction.json.gz` / `provider_observation.json.gz`。这不会自动收集延迟云日志或触发平台选择。

## 可复现实验

```bash
# 全程读取本地已有证据，不调用云服务。
.venv/bin/python -m atsuite.provider_model.experiment \
  --e1a results/phase4/e1a-formal-useast-seed11-v1 \
  --output results/provider-model/e1a-v3

# 任意受控实验可提供上述归一化记录的 JSON 数组。
.venv/bin/python -m atsuite.provider_model.experiment \
  --observations observations.json --output results/provider-model/validation

# 仅生成实验设计，不提交付费任务。
.venv/bin/python -m atsuite.provider_model.experiment \
  --plan --output results/provider-model/plan

.venv/bin/python -m unittest tests.test_provider_model tests.test_workload tests.test_analysis_v2 -v
```

验证按各 Provider 到达时间独立划分前 60% / 后 40%，避免 E1-A 后端顺序执行造成某个 Provider 没有未来样本。预测与更新同刻时先预测；本地旧数据的完成即采集是明确标注的事件时间模拟，云端日志按实际采集时间加入。固定 Capture 特征在请求前已知；通用观测验证只能使用之前已完成的 Profile。对同一批未来请求比较持续更新与训练截止后冻结的模型，不保证每个 Provider 的误差都会下降。

输出 `validation.json`、`model.json.gz`、`provenance.json`、`validation.png`、`load-sweep.json`、`cost-crossover.png`。包含延迟 MAE、p95 覆盖率、模型更新对比、成本分项估计误差和真实费用验证样本数。`crossover_intervals()` 只比较同币种、同 scope、完整窗口成本，返回负载扫描的交叉区间；外推点及分项费用不生成完整成本交叉结论。

受控计划使用 1 个基线 + 状态压力、LLM idle、负载、实例数各 2 个单因素变体，共 9 条件 × 4 后端。配置值不是测量值：状态/idle 变体先生成并封存等价 Capture，固定 DataSciBench 数据、LangGraph（项目已 pin 1.0.5）、Tool 与镜像版本；每个后端使用同样的捕获决策和到达序列。实例条件必须由实际平台支持并读回，不能执行的条件明确跳过。真实 Replay 继续使用 `static_deployment_runner.execute` 的 Pilot、`reserve_budget` 和基于证据的 admission；本次没有启动任何新云端实验。

历史 E1-A 固定同一任务、数据和 Tool/Graph 版本，但没有完整逻辑 S_G/ρ、实际驻留实例数和独占完整账单。因此已实现的接口能够接收这些新证据，当前历史验证不能证明状态压力的云端因果影响、完整容量预测或完整费用交叉区间。`provenance.json` 保存源文件校验和、版本标识、排除记录和上述限制；旧镜像依赖的准确性仅限于历史所保存的证据。

## 本次历史验证结果

2026-10-29 离线运行 `e1a-v3`：2,888 条真实请求、12 个观测窗口，2,941 个源文件保留 SHA-256，未排除缺失终止时间的记录。各后端时间切点的同时到达请求统一进入未来集合，因此每个后端未来集合有 296 条请求。

| 后端 | 可评分成功请求 / 未来请求 | 更新模型 MAE (s) | 冻结模型 MAE (s) | 预测 p95 的实际覆盖率 |
|---|---:|---:|---:|---:|
| AWS Lambda | 296 / 296 | 0.391 | 0.404 | 82.8% |
| AWS AgentCore | 296 / 296 | 5.398 | 5.313 | 84.1% |
| Aliyun FC | 296 / 296 | 0.451 | 0.423 | 82.8% |
| Aliyun FC Session MCP | 202 / 296 | 0.607 | 0.609 | 91.1% |

误差只评价成功请求；Session MCP 的其他未来请求不能因为未进入成功分布而被忽略。持续更新在 Lambda/Session MCP 上略有改善，在 AgentCore/Ali FC 上没有改善。经验 p95 的实际覆盖率低于 95%，当前模型不具备尾延迟 SLA 保证。所有后端的完整实际费用验证样本均为 0，费用曲线仅表示各自 scope 的使用量估算，完整费用交叉区间为不可验证。

首次验证相关 74 项测试通过。当时全量测试有 2 个 E1-B 数据缺失错误和 1 个 MCP 并发超时；以下记录后续修复与复验，不能把首次结果当作当前状态。

## 2026-10-29 证据补齐与并发修复

用户补齐 E1-B 数据后，两项测试通过。MCP 超时来自 `asyncio.to_thread` 的默认执行池：本机只有 12 个线程，而同 Session 的并发读测试需要 16 个调用同时进入屏障。现改用已安装 Starlette 的 `run_in_threadpool`，复用框架有界线程池；未改变读共享、写互斥规则。MCP 7 项回归通过，最终全量运行 194 项测试，无失败，1 项因宿主机缺少 sklearn/joblib 而跳过。该跳过项是 native 数值沙箱集成测试；另执行了下述固定镜像 Docker 真实 Replay，不将两者称为同一个测试。

普通 Capture/Replay 现在无论是否提供在线模型，都会保存 `workload_profile.json.gz`。旧 ATG 导入器保留 Tool 返回的完整 `logical_state.accesses`；不完整记录仍为未知。E1-A 结果采集器保存 Profile 及 S_G、D_G、ρ，并统计覆盖率。

新增本地证据位于 `results/provider-model/evidence-refresh/docker/{mcp,faas}-replay/`。复用原 `csv_excel_34` 决策、数据、seed 和既有 PNG 等价规则，执行 MCP、FaaS 各一次；不调用 LLM API。使用本地固定镜像 `sha256:e67088d55732b60825796a0e4aed693d59fa1fe1aeef87204cfa0bb19a1c5d5f`，核对镜像中的 `logical_state.py`、`sandbox_worker.py` 与当前源文件 SHA-256 相同。两个请求均成功，分别有 7/7 个 Tool 调用提供完整逻辑状态访问记录：

| 指标 | MCP | FaaS |
|---|---:|---:|
| S_G (bytes) | 20,171,094 | 20,171,094 |
| D_G (s) | 8.1197176508 | 8.1197176508 |
| ρ(G) (kB/s，十进制) | 2484.211258 | 2484.211258 |

这些数据验证仪表与传递链路，不是历史 2888 条云端请求的补测值，也不证明不同 ρ 条件下的性能趋势。历史结果与图表保持不变。要获得新的云端验证，需要部署包含当前仪表的镜像，封存带完整逻辑访问量的新 reference（可复用原决策做仪表化 Replay），更新其校验和与版本记录，然后先执行小规模 Pilot。直接使用旧镜像重跑不能保证补齐。正式扩展仍经过已有预算 admission；若要验证 ρ 的影响，还要覆盖不同状态压力和 idle 条件。

账单刷新保存于 `results/provider-model/evidence-refresh/`。阿里云 2026-10-28 日账单中，实验 FC Session MCP 函数为 CNY 4.565，FC 函数为 CNY 0.7183，对应 SLS 为 CNY 0.0053132 / 0.0051152。这些是供应商返回的资源日账单金额，仍需使用记录证明窗口归属和费用覆盖；不能按请求数直接均分冒充实际单请求费用。AWS 同日 AgentCore 为 USD 0.9880529251，但 Cost Explorer 标记 `Estimated=true`，且是账户服务日合计。Lambda 日金额为 0 不代表没有使用量；本次没有据此生成零成本模型。

离线证据审计恢复了 722 个 AgentCore Session 的用量采样时间和 `time_elapsed_seconds` 汇总；阿里云只保留匹配本窗口请求 ID 的实例活动区间，排除 SLS 查询时间窗内的其他请求。观测到的实例 ID 数不是同时驻留实例数，活动时间跨度不是完整生命周期；不同窗口存在重叠，不可直接相加。历史成功请求的 S_G、ρ 覆盖仍为 0，完整实际单请求成本仍不可验证。

新增运行时元数据记录进程标识、观测起点、运行时间和可获取的平台环境标识；MCP 正常启动/退出另发出 `runtime_started` / `runtime_stopped`。本地 MCP Replay 已验证两个事件。进程退出不等于供应商停止计费，强制终止也可能没有退出日志。FaaS 返回进程元数据，不伪造销毁事件。要证明完整驻留与保留费用，仍需平台实例创建/释放记录、完整 usage 日志和账单归属；当前模型不把进程时长当作 `service_slot_s`。

离线审计可复现，不访问云端：

```bash
.venv/bin/python -m atsuite.provider_model.evidence \
  results/phase4/e1a-formal-useast-seed11-v1 \
  --bill results/provider-model/evidence-refresh/aws-billing-20261028.json \
  --bill results/provider-model/evidence-refresh/ali-billing-20261028.json \
  --output results/provider-model/evidence-refresh/audit.json
```

`audit.json` 保留源文件校验和、每窗口逻辑指标覆盖、观测驻留边界及账单证据类别；不会修改历史结果或自动分摊共享费用。新增证据测试覆盖重复日志、缺失采样、非匹配实例、逻辑访问完整性和进程标识语义。本次没有部署新云端镜像或发起云端 Replay。
