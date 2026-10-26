# 六个候选的真实 Agent Trace

截至本次补充，六个已准备候选都有真实 DeepSeek Agent Capture 和重新执行 Tool 的 Replay。新增 human_142、human_7、csv_excel_29；既有三份记录及哈希未改写。不是 Phase 1 手写参考实现，也不是模拟模型决策。

新 Capture 使用官方 `https://api.deepseek.com`、请求/返回模型标识 `deepseek-flash`，日期 2026-10-26 UTC。LangGraph 运行实际模型产生的计划及 REVIEW/REPLAN；MCP 每个 Session 一个常驻内核，FaaS Replay 按调用恢复/持久化状态；数据和依赖均已准备，计算容器网络关闭。没有云端部署或镜像推送。

| 任务 | 迭代数 | 真实模型调用数 | 独立检查 | FaaS Replay |
| --- | --- | --- | --- | --- |
| human_142 | 2 | 3 | 30000 clients; 363 itemsets; 850 rules; selected k=4 | 通过 |
| csv_excel_29 | 1 | 2 | 151 training days / 49 forecast days; 300 observed optimizer steps; checkpoint reproduces forecasts | 通过 |
| human_7 | 1 | 2 | 1067371 source rows; 106 itemsets; 3 rules; three PNGs embedded in PDF | 通过 |

Capture 及 Replay 的模型请求消息、业务参数、Python 状态和所有声明文件的签名检查均通过。ATG 保留真实依赖和并行边；新增三份均可被 ATSuite Trace 解析。LSTM 没有为了并行度复制计算。

统一索引为 `benchmarks/DataSciBench/trace/captured/manifest.json`。新目录包含 events.jsonl、result.json、trace.atg.json、provenance.json，以及该次 Capture 使用的评测对照文件。Prompt SHA、原始仓库 Commit、输入哈希、模型 Response ID/usage、实际代码/参数、UTC/单调时钟、失败、REPLAN 和实际 Tool 输出均保留。镜像固定为 `sha256:c11ba32bbcd3cd398de643553593ac127833419743e3e5cb5eeb992a34796588`。汇总及实测并行区间：`results/phase2/capture-six-validation.json`。

Retail 读取全部 1067371 条原始记录，在声明的正数量/正价格/非取消范围内保留 1041670 条、40077 个 baskets。没有缩小数据集或以等待拉长计算。独立 validator 重新读取原始输入校验，开销作为实际 validate_result 调用保留；本轮是正确性验证，不能将这些本地耗时或 Docker 启动当作云端费用。

Credit 首个模型计划引用了运行约束而非原始 Prompt，按既有规则被拒绝后，实际 DeepSeek REPLAN 成功；未篡改原始决策。Retail 另外一轮三个计划均未通过证据校验，该轮保留为失败诊断，不计入接受的 Trace。仅修正该任务的运行提示，强调证据须来自原始 Prompt，再重新调用模型采集；没有改写原始 Prompt、伪造计划或放松证据校验。

LSTM 的实际模型代码选择 NumPy/Torch seed=42，尽管 Harness 配置 seed=0；两种值如实保留，Replay 执行完全相同代码并复现结果。训练 300 次实际 optimizer.step、保存权重哈希与观察到的最终参数一致，loss 从约 1.019 降到 0.0203。49 天预测 RMSE 约 0.1711，末值 baseline 约 1.0338；这些是对原始数据未来区间的报告指标，不等于官方 GT MAE。原始 dataset 为官方发布的 human-written 数据，不称为生产时间序列。

验证：82/82 单元和兼容性测试；Docker 自检覆盖 support/confidence/lift 重算、篡改拒绝、标准 JSON itemset 解析、真实优化更新，以及跨文件名稳定的 Torch checkpoint 数值签名。真实三个任务的成功 Capture/Replay 是结果正确性的主要执行证据。

官方 21 项指标的对应关系仍由 `tasks/evaluation_alignment.json` 与 `tasks/EVALUATION.md` 明确界定。新增 validator 强于单纯文件存在，但未执行官方 GT 集合覆盖、预测 MAE 和 VLM judge；所有官方通过标志保持 null。分箱、清洗、schema、支持度/置信度及 checkpoint 证据格式属于显式 Benchmark 契约，不冒充官方要求。
