# Phase 2 验证记录

新增 human_142、human_7、csv_excel_29 已完成真实 Capture/Replay，当前共六项；补充记录见 [CAPTURE_SIX_VALIDATION.md](CAPTURE_SIX_VALIDATION.md)。下文保留此前三项生命周期验证的原始证据。

本轮已将计算状态生命周期分开：MCP 每个 Session 一个常驻计算容器/内核；FaaS 每次调用恢复状态，并由写入调用持久化。LangGraph、动态 Send 并行、结构化工具、实际 Tool 输出校验、DeepSeek 决策 Capture/Replay 和 ATG 导出保留。

旧的“两种部署都逐调用启动计算容器”实现已被替换。旧 Capture 原始事件、真实模型响应、Token 和文件哈希保留，作为历史决策来源；本轮重新执行 Tool，不把旧的耗时当成新生命周期的测量。原有 DataSci 和 AgentTools 复用 DataSci/kernel.py，旧 FaaS 的超时前输出仍然流式保留。

## 最终环境与证据

- 范围：wlh-190 Docker；未部署、推送镜像或测量云账单。
- 最终计算镜像：`sha256:62e2b7e98e29e6bb21357787dff0c7bbedd49c3331ab174dac294c7df658e14b`。
- 单元/兼容性测试：82/82 通过，日志 `/tmp/wabber-resident-unit-complete.log`。
- 16 个 MCP Session 各有独立 RPC 进程与计算容器；屏障保证全部准备完成后才开始分析。不同数值输入及文件跨调用连续，Session 不串状态，同 Session 并行读取有真实执行重叠。分支修改不污染已提交对象，异常后仍可读取原状态；没有 SDK 状态快照及 canonical Python checkpoint。
- FaaS 检查恢复真实数组、文件连续性、独立调用容器、写入持久化，以及只读调用前后 SDK snapshot 字节相同。
- 独立 Docker 对抗检查验证未发布 merge 不可见、abort 保留旧 heap、commit 生效、过期 token 拒绝、只读状态/命令队列保护、外网及父内核信号隔离、超时后驻留数组仍可读取。FaaS 原有四组隔离检查也通过。

证据索引：

- `results/phase2/resident-replays-0d6f96a178b941bc8af4d27bf13983d1/lifecycle_summary.json`
- `results/phase2/lifecycle-96b61961d6504038b7805757a5f32789/validation.json`
- `results/phase2/resident-security-da81793ad79740aabffdedbd9b059cd4.json`
- `results/phase2/sandbox-security-edb5970bdf19467fb1702d73973aa142.json`

测试中的合成数组和超时 busy-loop 只用于回归/隔离检查，不计入真实任务 Benchmark。

## 三个真实 Prompt 的两种生命周期重放

复用归档中的真实 DeepSeek 决策及记录延迟，仍运行相同 LangGraph；所有 Tool 重新计算并检查实际状态/产物签名。此次回归不产生新的 LLM API 请求。human_15 保留原 Trace 的第一次验证失败及真实 REPLAN，不跳过失败或改写路径。

| 任务 | 部署语义 | 全部计算容器数量 | 已观测恢复 / 持久化次数 | 生命周期指标已记录 / 缺失 | 结果 |
| --- | --- | --- | --- | --- | --- |
| human_15 | faas | 14 | 13 / 4 | 13 / 1 | 通过 |
| human_15 | mcp | 1 | 0 / 0 | 13 / 1 | 通过 |
| human_12 | faas | 8 | 8 / 3 | 8 / 0 | 通过 |
| human_12 | mcp | 1 | 0 / 0 | 8 / 0 | 通过 |
| csv_excel_34 | faas | 6 | 6 / 2 | 6 / 0 | 通过 |
| csv_excel_34 | mcp | 1 | 0 / 0 | 6 / 0 | 通过 |

次数只统计实际 Python canonical snapshot 的加载/保存，不包含分支增量传输；SDK 对象/文件的恢复和持久化另见事件内 transport 的同步指标。失败验证调用有未记录的细分状态指标，明确列为缺失，不能补零或将部分总和当成完整请求费用。未发生恢复/保存的耗时为 null；成功 MCP 调用的实测标志为不执行 canonical restore/save。完整 RPC 总时间及失败记录仍保留。

真实并行例子是 Adult 共享清洗结果后的分类模型与聚类，以及 Census 清洗后的 Lorenz/Gini；没有复制相同任务制造并行。各次实测交叠区间、独立校验、容器标识及源码 SHA256 见 lifecycle_summary.json。

## 官方评测对应关系

六个已 profiling 候选的全部 21 项官方指标已逐项映射至自定义检查，固定原仓库 Commit、Prompt SHA、metric.yaml SHA 和每项代码 SHA。`uv run python -m experiments.exp1.data.validate_evaluation_alignment` 离线复核通过。说明见 [EVALUATION.md](../../benchmarks/DataSciBench/tasks/EVALUATION.md)。

本轮选择“明确自定义检查与官方要求的对应关系”，没有冒称接通全部官方 evaluator：未验证 GT 准确率/轮廓系数阈值及 VLM 分数；.80、列名、固定划分和清洗人口等额外契约已明确。后续已补齐另外三个候选的真实 Agent Capture 与跨运行时 Replay，详见 [CAPTURE_SIX_VALIDATION.md](CAPTURE_SIX_VALIDATION.md)；本页表格保留本轮生命周期改造时的三项测量，不能混为同一批耗时。运行状态新增 evaluation_basis，官方通过标志保持 null。

## 使用范围

MCP 的 2 CPU / 4 GiB 是 Session 总预算，FaaS 是每次调用预算。两种预算和并行容量必须在正式策略实验中显式配置/解释，不能直接从此正确性回归推断价格优劣。

当前 FaaS 验证器仍逐调用启动本地隔离容器，其启动开销与真实云端 warm invocation 不等价；已有 exec 区间、sandbox 总耗时、SDK 同步和 RPC 总时间供分层分析。这里验证的是生命周期及计算正确性，不把 Docker 启动或本地磁盘恢复当作云平台冷启动、对象存储网络延迟或实际费用。云端正式测量仍需 Provider 原生隔离执行器及真实账单。MCP 隔离要求 Linux Landlock ABI >= 6，缺少能力时失败，不降级。
