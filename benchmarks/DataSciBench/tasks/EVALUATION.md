# 自定义检查与官方评测的对应关系

当前结果表示自定义 Prompt 检查通过，**不表示官方 TMC/CR 评测通过**。官方分数和通过标志保持 null。运行结果 `evaluation_basis` 记录检查方式、官方源 Commit 和本清单 SHA256。

官方源固定为 `THUDM/DataSciBench@84ef3d4d94d7362a5149cf14a73dc168fc4f2f33`。官方入口为 `experiments/evaluate.py`，指标定义为 `metric/<task>/metric.yaml`。已逐项核对六个候选的全部 21 个指标；机器可读的指标名称、官方代码 SHA256、ground_truth、已覆盖内容及缺口见 [evaluation_alignment.json](evaluation_alignment.json)。使用原始 `data/<task>/prompt.json`；部分 metric.yaml 内嵌的旧 prompt 与它不同，不据此更改原始 Prompt。

| 任务 | 当前检查与官方要求的关系 | 尚未满足的官方条件 |
| --- | --- | --- |
| human_12 | Agent 重算五项预测指标、检查 held-out 标签、重载模型、检查聚类和 PDF；参考实现另检查缺失值及 cluster profile | 官方按 GT 比较缺失比例、准确率及前 1000 行轮廓系数；当前未比较。`>= .80` 是额外门槛，不等于官方准确率门槛。预测/聚类列名也与官方不同 |
| human_15 | Agent 检查实际图形几何、年销售总量、红色/黑边、三条 dash-dot 线及散点数量/大小/透明度 | 未运行官方 VLM 的轴、标签、配色、图例和布局比较；未逐点核对时间/类别映射。官方散点代码要求 Prompt 未要求的辅助 CSV，且未配置该项 GT |
| csv_excel_34 | Agent 对照输入验证清洗后的完整收入样本，独立重算 Gini，并解析 PNG | 未与官方 GT 比较行数；Gini 使用数值容差而非官方精确相等；没有检查 Lorenz 坐标，也没有 VLM 评分 |
| human_142 | Agent 与参考实现：从原始 baskets 重算 support/confidence/lift，重算 k=2..8 惯性及选定模型的全部客户端分组，检查报告 | 未比较与 GT 的规则/itemset 集合命中比例 >= .9；官方额外要求的 customer_segmentation_analysis.pdf 不在原始 Prompt 中，未添加假文件凑分 |
| human_7 | Agent 与参考实现：从原始两张表重算 itemset/rule 指标、全部产品/国家/月收入，并验证 PDF 实际嵌入三张图 | 未验证官方 GT itemset recall >= .9；有效 support 不等于官方集合覆盖率 |
| csv_excel_29 | Agent 观察真实 optimizer 更新及参数哈希，重载 LSTM 复现 49 个预测；参考实现另有独立 gate 重算及 holdout RMSE | 未比较官方 GT 预测 MAE < .1，未运行 VLM；官方额外检查的 cleaned_data.csv 不在原始输出要求中 |

`covered` 只用于自定义检查能蕴含官方字面的文件存在条件，不代表该任务所有官方指标通过；`partial` 表示有关联但无法推出官方通过；`not_covered` 明确表示未覆盖。

额外契约包括 Adult 固定 train/test 行数、`split/target`、`y_true/y_pred/y_score`、checkpoint 字典结构和 .80 门槛，以及 Census 保留全部有限非负收入。这些用于当前 Benchmark 的确定性与防止错误计算，不冒充官方要求。当前 Adult Agent 检查未独立复核预处理是否只在训练集拟合；图表内容完整性和报告解释质量也有缺口。新增三个任务已接入 Agent validator 并完成真实 DeepSeek Capture；验证记录与 Replay 索引见 trace/captured/manifest.json。Credit/ Retail 的分箱、清洗与 support/confidence 阈值，以及 LSTM checkpoint/CSV 的证据格式也是显式 Benchmark 契约，未替换原始 Prompt 或冒充官方要求。

官方 VLM 在 `src/vlm_utils.py` 使用 gpt-4o-mini 和第三方 API endpoint。为了保持 Tool 离线，本轮选择显式对应关系，不把该在线 judge 接入计算 Tool，也不以自定义几何检查伪造 VLM 得分。未来需要官方可比得分时，应在测量之外单独准备并校验 GT、适配列名/辅助文件、运行 evaluator 并保存逐项结果。

离线复核源版本、Prompt SHA、全部指标数量/名称/代码 SHA 和空的官方分数：

```bash
uv run python -m experiments.exp1.data.validate_evaluation_alignment
```

该命令只用 `git show` 和 `yaml.safe_load` 读取定义，不 eval/执行原仓库代码，也不下载数据。实际数值正确性仍由 Tool 中的检查及真实 Capture/Replay 验证；此命令检查的是对应清单没有漏项或错引版本。
