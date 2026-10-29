# Online Workload Characterization

本模块固定使用 DataSciBench + LangGraph，单进程共享一个 `WorkloadModel`。
不按 Prompt 分组：不同 Prompt 是同一 Workload 的观测样本。无 Provider 预测、路由或扩缩容。

## 接口与接入

```python
from atsuite.workload import WorkloadModel

model = WorkloadModel()
# 在执行 Agent 之前调用；返回值是独立快照。
prediction = model.begin(request_id, session_id)
# 执行完后传入 TraceCollector.snapshot()；失败也必须提交以结束活跃 Session。
profile = model.observe(trace)
model.save(path)                  # 原子写入 path.json.gz（path 本身应为 .json）
model = WorkloadModel.load(path)
```

`begin` 返回当前请求在内的活跃 Session 数；Session ID 去重。
`observe` 自动提取 Profile 并更新统计。未完成请求保留作审计，后续允许替换为完成结果。
已完成的相同 Profile 重复提交幂等，冲突结果报错。预测方法只接受时间截止点，不接受新请求的 ATG。
`predict(as_of_ns)` 仅纳入 `available_ns < as_of_ns` 且完成时间早于截止点的样本。
实时 `observe` 的 availability 默认为入库时的 UTC 纳秒时间；延迟提交不会倒填到完成时刻。
手动传入时间仅用于受控回放，调用者必须保证它确实是观测可用时间。

DataSciBench 的现有 Capture 入口支持：

```bash
.venv/bin/python -m experiments.exp1.capture.capture_agent_traces \
  --task human_7 --config benchmarks/DataSciBench/config/langgraph_experiment.json \
  --output results/new-capture --workload-model results/workload/model.json
```

它在请求开始前保存 `workload_prediction.json.gz`，结束后保存 `workload_profile.json.gz`，并更新模型。
同进程并发调用 `run(..., workload_model=model)` 可共享统计；不要让多个进程同时写同一个模型文件。
原有运行方式无需模型参数，行为保持兼容。任务结果失败与 Graph 正常返回分开处理。

## 统计口径

- `llm_idle_s`：LLM 调用观测耗时之和，包括已记录的重试等待；并发调用相加，不等于请求墙钟时间。
- `tool_elapsed_s`：Tool 调用耗时之和；不冒充 CPU 计算时间。
- `tool_cpu_s`：只有所有 Tool 均提供 `resources.cpu_seconds` 才给出总和。
- `agent_elapsed_s`：Session 执行段耗时之和，不包含 checkpoint 暂停期间的人为等待。
- `state_bytes`：逻辑上需要外置的状态读写量之和；同一对象重复访问重复计数。
- `rho_kb_s = state_bytes / 1000 / llm_idle_s`。缺失状态观测、缺失时间或零分母时为 null。
- `state_reads` / `state_writes` 是对象访问次数；`state_max_object_bytes` 是观测中最大单次对象大小，不是总常驻内存。

每个 Tool 和启用 checkpoint 的 Session 都需要提供完整逻辑访问清单：

```python
collector.observe_state_accesses(span_id, [
    {"object_id": "workspace/state", "operation": "read", "bytes": 2048},
    {"object_id": "workspace/state", "operation": "read", "bytes": 2048},
    {"object_id": "workspace/state", "operation": "write", "bytes": 4096},
])
```

清单对应 `span.metadata.state_accesses`。仅当确定无状态访问时传 `[]`。
对象粒度及序列化方式必须在固定实验环境内一致；不支持观测时不要猜测或传零。
MCP 内存常驻与 FaaS restore/persist 使用相同逻辑口径，不需要额外执行对象存储读写。
现有 `state_read_bytes` / `state_write_bytes` 以及 summary 中的 `state_transfer_bytes` 继续代表实际传输。
Trace summary 的 `rho_kb_s` 已改用逻辑需求，新增 `workload_state_bytes`；旧调用者不能再依赖传输计数推导 rho。
DataSciBench Runtime 现在自动生成 `logical_state`，TracingAdapter 将完整观测导入
`metadata.state_accesses`，不需要调用方手动补写。历史 Capture 不会自动补齐；旧 worker、执行失败或
序列化测量失败仍然保留缺失，绝不将部分观测伪装为完整需求。

Workflow 使用与 ATG 导出相同的 span 因果依赖，保留调用 DAG，不按完成顺序串联并行节点。
概念节点按名称聚合，提供出现概率、调用次数经验分布、依赖边频次、后继集合频率和并行峰值。
显式 routing metadata 优先；否则后继集合从已执行 DAG 推导。后继集合概率是每次节点执行的经验频率，
包括终止和并行 fan-out，不表示每条边是互斥分支。循环使用“存在同名因果祖先”的重复执行次数，
并行同名节点不算循环；嵌套同名节点仍聚合在一起，不尝试恢复源码中的循环语句。

只用完整且请求成功的样本预测完整 Workload；失败和不完整数量独立报告。
可恢复的内部 Tool 错误不自动使最终成功请求失败。每个指标分别统计有效样本数、缺失数、覆盖率、
均值及 p10/p50/p90，保留全部经验样本。rho 分布直接来自逐请求比值，不能用平均状态量除以平均时间代替。
少于 10 个样本、存在缺失时标记低置信度；Capture 初始化整体也标记低置信度。
p10/p90 是经验预测范围，不是统计置信区间，不提供未经校准的置信概率。

## 可复现实验

```bash
.venv/bin/python -m atsuite.workload.experiment \
  benchmarks/DataSciBench/trace/captured --output results/workload-characterization
.venv/bin/python -m unittest tests.test_workload tests.test_tracing tests.test_tracing_continuation -q
```

六组归档 Capture 使用仓库 manifest/provenance 固定的环境和数据，不调用模型、不部署云服务。
优先读取统一 Trace；旧 Capture 从 ATG 和事件对齐恢复调用依赖及时间，无法恢复的状态观测保持缺失。
采用事件时间模拟到达/完成：同一时刻先处理到达，再处理完成，交叠请求不能互相偷看结果。
旧事件末尾时间用于模拟完成，不声称还原真实历史入库时刻。输入文件不被改写。

输出：

- `validation.json`：逐请求预测、真实 Profile、有效指标的中位数绝对误差、最终模型。
- `model.json.gz`：可重新加载的模型样本。
- `distributions.png`：四项主要指标的经验 CDF、预测误差、随历史样本数变化的分位数。
- `workflow.png`：常见节点的平均调用次数；完整依赖及分支信息保存在 JSON 中。

本地归档验证得到 6 个成功样本，真实 rho 有效样本数为 0，图中明确显示缺失。
自动测试额外使用确定性 Trace 验证正值 rho、重复读写、常驻/外置口径一致、动态 fan-out/循环、
重叠请求、延迟入库、失败和不完整请求、持久化及重复提交。

这是保留所有样本的 PoC，内存及查询开销随样本数增加。当前不做数据库、采样压缩或环境版本管理；
模型文件应只承载同一固定环境的 Workload。归档回放用于验证统计逻辑，不是新的真实 LLM/Docker 性能测量。


## MCP/FaaS 统一状态边界

边界名为 `datasci-tool-snapshot-v1`。它是每次 Tool 调用取入/提交的完整状态快照，
不是 Python 变量读取次数，也不是文件 `open()` 次数。分析代码可以访问整个 canonical Python 状态
和可见 artifact 快照，所以即使某个字段或文件未被程序实际使用，也按快照粒度计入。
重复调用读取同一版本仍重复累加，合并后写入完整新版本；不能同时再累加 SDK restore/save 的传输计数。

| Tool | 逻辑读取 | 逻辑写入 |
| --- | --- | --- |
| prepare_workspace | 初始 workspace 元数据 | 初始化的空 canonical 状态、更新后的 workspace 元数据 |
| execute_analysis | workspace 元数据、canonical 状态、全部可见 canonical artifacts | 分支 state_out、声明的输出 artifacts、分支语义元数据 |
| merge_artifacts | workspace 元数据、旧 canonical 状态与 artifacts、每个分支状态/artifacts/语义元数据 | 新 canonical 状态、完整新 artifact 快照、workspace 元数据 |
| read_workspace | workspace 元数据 | 无 |
| validate_result | workspace 元数据、canonical 状态、全部可见 canonical artifacts | 无（校验用临时副本不构成跨调用状态） |

计量规则：

- canonical Python 状态使用固定 `joblib.externals.cloudpickle`、protocol 5 序列化后的长度。
  MCP 仅在沙箱内部序列化到内存以测量，不恢复/保存 canonical `state.pkl`，也不新增对象存储流量。
  FaaS 的实际 restore/persist 行为保持不变。分支状态使用已生成的 state.pkl 字节数。
- workspace 元数据固定为 version/task_id/inputs/artifacts；分支语义元数据固定为
  artifacts/state_signature/state_names。都使用排序键、紧凑分隔符的 UTF-8 JSON 字节数，排除 PID、耗时等运行时字段。
- artifact 使用原始文件字节数。同一个文件在分支写出、合并读入和 canonical 写出是三个独立访问。
- 固定数据集、请求参数、stdout、验证临时副本、追踪信息、tar/gzip 封装、SDK 锁及传输重试不属于这一定义的状态对象。
  `read_workspace` 返回的旧 `state_bytes` 字段仍是物理 state.pkl 大小，不用于计算逻辑需求。
- 当前固定 Agent 不启用 LangGraph checkpoint；若以后启用，还需要单独观测 checkpoint，不能把缺失当零。

`logical_state` 同时提供 `complete`、`errors`、`observation_s`、`observation_cpu_s`。
观测失败不会因新增统计而中断本来可执行的 Tool，但该请求的状态总量和 rho 变成缺失。
错误/中断执行保留已知访问，业务失败与观测完整度分别判断。只读 validate_result 在完成输入状态观测后校验失败，仍有完整的读取需求；未知的部分执行仍保持缺失。
`state_observation_s` 汇总到 Workload Profile；原始 Tool 墙钟和 worker CPU 计时不被悄悄扣减，
因此仍包含观测开销。该时间主要覆盖测量/序列化，不宣称涵盖统计代码的全部管理开销。

### 本地双路径验证

```bash
ATSUITE_TEST_ARTIFACT_DIR=results/logical-state-validation \
  uv run --no-sync --with joblib==1.5.3 --with scikit-learn==1.3.2 \
  python -m unittest tests.test_logical_state -v
```

测试使用实际 NativeKernel、SDK execution 和五种 DataSciBench Tool，运行确定性的图表生成代码，
并调用 human_15 的独立结果校验；状态包含 NumPy 数组和 Pandas DataFrame。
LLM 耗时使用明确的固定测试值 2 秒。
覆盖 MCP 常驻堆、FaaS 逐调用恢复/持久化、重复读取、失败调用、并发日志隔离和旧 worker 缺失。
两条路径的事件逐项一致，7 次成功 Tool 调用合计 30 次读取、20 次写入，S_G 和 rho 完全一致。
输出证据保存在 `results/logical-state-validation/logical-state-parity.json`；它是本地集成证据，
不是云部署或真实 LLM 性能实验。使用同一依赖环境比较字节数，不跨序列化库版本比较。

新模块已加入沙箱 Dockerfile 和部署镜像源码审计。已有固定镜像不会自动获得新代码，
需要重新构建并更新部署的镜像 digest/expected_implementation_sha256 后再采集；旧 Capture 的缺失不倒填。


### 真实 LLM + Docker 验证

已运行原始 human_15 Prompt，使用真实 DeepSeek API、LangGraph 和本地 FaaS-style RPC/Docker。
本机缺少旧参考镜像，因此另建独立验证镜像，固定 Python 3.12.12 base digest 和仓库数值依赖；
完整依赖快照、构建上下文、输入哈希及源码哈希保存在 results/live-workload-docker-fixed。
该结果不与旧参考镜像的性能数值混用。

- 4 次 live_official_api LLM 调用，15 次真实 Tool 调用，最终任务和图表独立校验成功。
- 136 次逻辑访问（89 read / 47 write），S_G = 1,695,718 bytes。
- D_G = 17.399019309 秒；rho = 97.460550499 KB/s；全部 Tool 的逻辑观测完整。
- 首请求预测使用 0 个历史样本，完成后模型成功样本数为 1，未使用事后 ATG 预测自身。
- 首次实测发现只读业务校验失败被误标为观测不完整；已修复，并在第二次真实请求中验证恢复分支。
  未完成状态读取、序列化测量失败、未知的部分执行仍不会生成完整 S_G。
- MCP Docker 尚未验收：宿主 Landlock ABI=4，现有隔离要求 ABI>=6；没有绕过隔离运行。

证据：`results/live-workload-docker-fixed/report.json`，原始 Trace/事件/最终状态在其 capture 子目录；
首次暴露问题的记录保留在 `results/live-workload-docker`。未部署或修改任何云端服务。


### 内核升级后的 MCP Docker 验证

宿主已启动 `7.0.0-42-generic`，实际探测 Landlock ABI=8。使用与上一轮 FaaS 相同的
`wabber-logical-state-validation:local` 镜像，镜像内 sandbox_worker.py / logical_state.py 哈希与当前源码一致。

真实 DeepSeek + MCP Docker 的 human_15 请求已通过：4 次 LLM 调用、19 次 Tool 调用，
最终图表独立校验成功。S_G=2,474,859 bytes，D_G=19.933775986 s，rho=124.154048974 KB/s，
所有逻辑访问观测完整。18 次计算调用使用同一 MCP Session 容器；所有带生命周期字段的调用均未执行
canonical restore/save，Session 目录中没有 canonical state.pkl 或 SDK .snapshots。
一次失败校验未携带 state_lifecycle 字段，作为独立可观测性缺失记录；它的逻辑状态观测完整，
不影响 S_G。验证脚本最初对该缺失字段错误地直接索引；修正检查后重新验证已保存的真实 Capture，
没有重发 LLM 请求或修改原始 Trace。原验证器报错记录保留在 initial-verifier-error.json。

此外，将上次 FaaS 的真实 Capture 按原决策重放到 MCP Docker，逐调用按 event.key 比较逻辑访问列表，
全部一致：两边 S_G 都是 1,695,718 bytes（89 read / 47 write），rho 都是 97.460550499 KB/s。
这次重放没有新的 LLM API 调用；它验证同一执行轨迹下的部署模式无关性，不把不同实时请求的 ATG 当作相同。
不跨内核或不同并发资源语义推导性能优势。

证据：
- `results/live-workload-mcp-docker/report.json`：真实 MCP 请求及生命周期检查。
- `results/workload-mcp-docker-parity/report.json`：相同轨迹的 FaaS/MCP 逻辑状态事件一致性。
- 两目录的 capture/ 子目录保存完整 Trace、events、result、开始前预测与完成后的 Profile。

测试结束后 RPC 前端与 MCP 容器均已关闭；未部署云端服务。
