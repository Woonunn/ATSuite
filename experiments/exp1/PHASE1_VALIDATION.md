# Phase 1 验证记录

当前保留 **172 条 Prompt，6 个实测候选，166 条待运行验证**。来源固定为 THUDM/DataSciBench Commit `84ef3d4d94d7362a5149cf14a73dc168fc4f2f33`。原先保留的 171 条全部保留，本轮仅补回已获得官方输入并验证通过的 csv_excel_29；其余 50 条仍排除。例外绑定来源 Commit 与 Prompt SHA256。

固定 CPU 参考镜像：`sha256:2ff27b3d2fdb85cb0ec0389b0f92df9ec9ef6ad042609364666c6ef4e833f437`。新增 torch==2.5.1+cpu 后，六项全部在同一镜像下重新测量，未混用旧镜像指标。Python 3.11.1，2 CPU 配额、4 GiB 内存、数值库单线程、seed=0、network=none；每项 3 个独立容器，18/18 成功。同任务所有输出文件 SHA256 跨三次执行完全一致。

| 任务 | 分类 | 计算步骤总耗时中位数/s | 最长步骤中位数/s | CPU 时间中位数/s | 全进程峰值 RSS/MiB | 共享状态最大字节数 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| human_15 | light | 0.493 | 0.203 | 0.499 | 221.6 | 977 |
| csv_excel_34 | light | 0.409 | 0.284 | 0.414 | 250.2 | 5462261 |
| human_142 | medium | 5.738 | 4.190 | 5.775 | 326.5 | 12683966 |
| human_12 | heavy | 16.830 | 9.696 | 16.963 | 928.4 | 266873451 |
| human_7 | heavy | 68.001 | 63.530 | 68.423 | 1023.3 | 92827172 |
| csv_excel_29 | light | 1.286 | 0.975 | 1.295 | 461.2 | 23819 |

步骤时间包含步骤自身文件 I/O，不含单独记录的状态序列化、步骤外最终校验和依赖导入。CPU 时间含采样线程，可略大于墙钟时间。RSS 为计算进程 ru_maxrss，包含 Python、库、序列化和校验开销；另保存 10ms 步骤 RSS 采样，不是容器 cgroup 峰值。共享状态为真实中间对象 pickle protocol 5 序列化大小，不等同于 SDK JSON、FaaS 传输量或平台费用。宿主机负载与页面缓存未隔离。

## 第 6 个候选：csv_excel_29

原始输入：`benchmarks/DataSciBench/data/csv_excel_29/data.csv`。从已获授权的 zd21/DataSciBench 固定 HF 修订 f15d3e4043498febc26d7bcb7562b7e344b514f2 下载，13981 字节、200 行，Date/Value1/Value2/Value3 四列；SHA256 `5c26a06faffc1438e79ed3e8a74bad42f6889c13eb4e36ed44994823650c28b3`，Git blob SHA1 `f36721fc0af242eea236bd2166e3987bc775dc77` 与上游不可变修订的文件元数据吻合。data_source_type=3：上游人工编写数据，不称作真实生产时间序列。

真实计算经 ATSuite datasci_run_code Tool 的 Python worker 执行。使用 2023-01-01 至 2023-05-31 的 151 个 Value1，14 天窗口、32 维单层 LSTM、Adam lr=.01；按时间留出最后 28 天选择 epoch，标准化仅拟合对应训练片段，随后按选定 epoch 用全部 151 天重新拟合。递归预测 2023-06-01 至 2023-07-19 的 49 天，预测时不读取未来真实值。

实测选择 30 epochs；训练归一化 MSE 从 0.972170 降至 0.039281。验证真实权重更新、CPU LSTM、精确 49 天日期、有限数值、模型重载复现全部预测，以及独立 NumPy LSTM 门公式复算首个预测。正式输出 predictions.csv、predictions_chart.png；辅助保留 model_checkpoint.pt、training_metrics.json。

防泄漏：在另一独立断网容器中把全部预测期 Value1 改为 1e9，并把 Value2/Value3 全部改为非数值字符串；模型 checkpoint、49 天预测和图表 SHA256 不变。改写数据仅用于负向检查，不纳入 Benchmark 输入或正式 Profiling。

预测质量限制：49 天实际 RMSE=1.200561，末值常量预测基线 RMSE=1.033847。当前参考 LSTM 没有优于该基线；作为轻量 PyTorch 运行候选入选，不声称预测质量占优。原 Prompt 未规定误差阈值，算法/训练/日期/输出正确性与预测质量分开记录。未来实际值仅在最终预测完成后计算这些报告指标，不参与模型选择。

该任务约 1.3 秒，低于 5–45 秒的优选重计算范围；未通过额外 epoch、无意义循环或等待拉长任务。其他任务覆盖中等、重计算和较大内存需求；零售 Excel 导入仍超过优选范围，实际值如表。

## 验证与证据

- 完整 Python 回归 71/71；断网 Docker 中 Importer、数据、发布及 LSTM 输入回归 23/23。测试容器仅为 Git fixture 只读挂载宿主 Git/链接库，数值计算镜像不变。
- 原五项继续校验：Adult 官方留出集与模型重载指标、聚类统计；PUMS 两种 Gini 公式；信用卡及零售全量 itemset/rule 计数、聚类与收入守恒；销售图表数据、样式及点数；PNG/PDF 可解析。
- 全部数值计算通过 ATSuite Tool；这些是人工编写的参考解法，不是 LangGraph/DeepSeek Agent 轨迹。DAG 标注依赖；LSTM 训练、选 epoch、最终拟合与递归预测依次执行，没有独立并行子任务。
- 镜像构建前准备依赖、测量前准备输入；容器执行时无外部网络、下载或在线安装。未部署云平台、推送镜像或生成账单。

正式证据：`results/phase1/20261025T151515Z-6035e855299c/batch.json`；本轮汇总：`sixth-candidate-validation.json`。每次执行保留 outputs/哈希、profile.json、events.jsonl、tool_execution.json、execution.json 与容器日志；包版本、代码哈希及输入路径见 manifest 和 datasets.lock。

下载溯源：`results/phase1/csv29-source-625c48c1f1de44bf81fe2d992cd75db1`。防泄漏证据：`results/phase1/csv29-leakage-b52c87199c224e28a6111ff91ee838a3/validation.json`。此前 5 项/171 条的历史结果保留于 results/phase1/selection-e5cf01aa4659453b85979b4f5e531111，未改写旧环境的测量记录。
