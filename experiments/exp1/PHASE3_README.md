# Copilot 真实到达时序

当前默认采用 **10 分钟**完整真实窗口，另支持 15/60 分钟；预算比较和三窗口放大图见 [WINDOW_SELECTION.md](WINDOW_SELECTION.md)。旧一小时数据和验证记录保留。

来源为 Azure/AzurePublicDataset 的 [ghcp-coding-agent-2026 release](https://github.com/Azure/AzurePublicDataset/releases/tag/ghcp-coding-agent-2026)：2026-06-01 至 06-07 的非企业用户 agentic Session 抽样元数据。保留原始归档，下载/解析不发生在计时运行期间。来源按 CC BY 4.0 归属 AzurePublicDataset；引用 Liu et al., *Agentic Coding in the Wild: Characterizing GitHub Copilot Traces at Production Scale*, arXiv:2608.00101。

`data/copilot_source.lock.json` 固定 release ID、tag Commit、独立文档 Commit、七个资产 ID/URL/字节数/发布方 SHA256。文档在 release tag Commit 之后加入，因此分别固定版本。准备程序校验归档 SHA、提取路径、每个 gzip shard 的 SHA/大小、官方 manifest 的完整标记、分区和逐 shard Session 数。失败时不会发布 arrivals.csv。

## 提取与清洗

每行 JSON 是一个 Session，读取 `session_id → turns[].turn_id → llm_calls[].timestamp / initiator_type / duration_ms`。全局按 Session ID 合并，取所有记录中的最早显式 `initiator_type=user` 调用；数组顺序或后续 Turn 不直接构成新请求。取消/失败的 LLM 调用也可能是真实用户到达，不按执行结果过滤。

清洗规则在全量解析前固定。本次按预算要求只调整选择窗口长度，保持清洗规则及分位选窗规则；分别记录 source_cleaning_policy_sha256 和 selection_policy_sha256。除 window_minutes 外，配置改变仍要求重新提取：

- 时间戳必须是带时区的 ISO 字符串，统一 UTC，最多保留九位小数；无时区、非法日期不猜测修复。
- 缺少发起标记的调用不会推断为 user。若未知发起类型在候选首个 user 之前或同一时间，或存在无法定位的非法 user/unknown 时间戳，排除该 Session，不提升后续 Turn 冒充首个请求。
- 无显式 user 的 Session 不符合此次到达定义；这不代表源 Session 是坏数据。全局最早 user 缺少 turn_id 也排除。
- 分区字段 date 必须匹配目录。首个 user 在分区日区间之外允许 ±24 小时的预定宽限，以保留跨午夜会话；超出宽限为明显异常。首个 user 不在完整观察周内时排除 Session，不改用周内后续 Turn。
- 无效/缺失 duration_ms 留空并计入清洗报告，不补零、不改变已知到达时间。该字段只是来源审计，不代替 DataSciBench 实际执行时间。

既有 arrivals.csv 的旧一小时 window_id 作为来源分组标注保留，数据文件和相对时间不改写；新选窗记录该标注的时长，读取器核对来源后把请求标识为新的 10/15 分钟窗口。重新提取时采用当前配置的分组时长。

`arrivals.csv` 恰有 arrival_id、original_timestamp_utc、relative_arrival_ms、source_partition、source_type、window_id 六列。相对时间以观察周 UTC 起点为零，用整数纳秒计算，再无损写为毫秒小数；保持所有原始间隔及并列时间。另有 `arrival_sources.csv` 保存 Session/Turn、原时间戳、duration、源 shard/行号。`cleaning_report.json` 记录计数、排除原因、诊断、源 manifest 和哈希。SQLite 是中间全局去重索引。

## 流量特征及选窗

`analysis/traffic.py` 输出 per_minute.csv、per_hour.csv、per_day.csv、interarrival_ms.csv、windows.csv、traffic_summary.json、traffic.png/pdf。零请求分钟/小时也计入完整观察期。

- 到达间隔保留零间隔，报告分位数、均值、总体标准差、CV，以及 B=(std-mean)/(std+mean)。
- 每个完整 UTC 对齐窗口（默认 10 分钟，亦支持 15/60 分钟）报告平均抽样到达率、每秒/每分钟峰值、峰均比和分钟计数 Fano factor（总体方差/均值）。
- 工作日/周末按 UTC 日期划分，并报告观察天数。这里只有一周，不能推断全年季节性。
- 在全部 1008 个完整 UTC 十分钟窗口上（15 分钟为 672 个、一小时为 168 个），按 `(arrival_count, start_utc)` 排序，分别取索引 `floor(q*(N-1))`，q=0.10/0.50/0.90，作为 low/typical/high。包含零窗口；不读取费用、Provider、SLO 或优势反转信息。

这些是**抽样且可观察到 user 发起的 Session 到达率，不是完整 Copilot 产品 QPS**。本次数据有大量时间戳并列和十秒边界对齐；零间隔和细粒度峰值反映发布数据的时间分辨率与抽样，不能解释成完整生产服务的精确瞬时突发。不注入抖动、Poisson 请求，也不估计未知采样率。

## Open-loop 调度

`runner/arrival_scheduler.py` 的 `run_open_loop` 接收 Arrival 列表及 async handler。发布循环按绝对单调时钟 deadline 投递，不等待先前 handler 完成。独立 worker 和有界队列限制执行并发；队满返回显式 rejected，异常为 failed，中断时剩余请求逐一标记 cancelled。正常运行保留尾部空闲区间并等待积压排空。最多 100 ms 的分段定时等待用于降低长空闲后的计时器合并误差，不 busy-spin，也不填充计算工作。

handler 必须在实际 Harness 入口调用 started()；本地探针在真正的 executor 线程开始时调用，不能用入队时间冒充 Harness 开始时间。handler 返回小型 JSON 结果或引用；数值数据不放进调度记录。

每个请求保留计划、实际提交、dispatch、Harness 开始、最终终态的单调时钟时间与 UTC 时间。`actual_arrivals.csv`、结构化 events.jsonl 和 summary.json 分开报告提交误差、Harness 启动误差、入队至 Harness 的等待、峰值队列/活跃请求、排空时间、提交缺失率、失败/拒绝率和未核销数。缺失时间为 null/空值，统计不补零。

默认 time_scale=1.0。任何非 1 缩放都必须显式 `--trace-scaled`，并在记录中标记 **Trace-Scaled Scenario**；执行偏移和窗口时长除以 time_scale，原始来源时间戳仍保留。不能把这种场景混入原速正式实验。

## 复现

从项目根目录执行：

```bash
uv run python -m experiments.exp1.data.prepare_copilot
uv run python -m experiments.exp1.data.extract_copilot_arrivals \
  --output results/phase3/arrivals-v1
uv run python -m experiments.exp1.analysis.traffic \
  --arrivals results/phase3/arrivals-v1/arrivals.csv --output results/phase3/traffic-10min-v1
uv run python -m unittest discover -s tests -q
uv run python -m experiments.exp1.tests.validate_copilot_timing \
  --traffic results/phase3/traffic-10min-v1 --output results/phase3/timing-10min-v1 \
  --image sha256:c11ba32bbcd3cd398de643553593ac127833419743e3e5cb5eeb992a34796588
```

输出目录必须不存在，防止覆盖证据；重复验证使用新的目录名。已下载、哈希相同的资产会复用，不重复下载。源数据和结果均在 Git 忽略的 results/ 内，代码及固定规则独立保存。

最后一条在 wlh-190 的三个断网 Docker 中并行验证，各为 2 CPU/2 GiB，使用已有本地镜像，不构建或推送云镜像。每个已选窗口只执行**预先固定的前 120 秒验证切片**，默认原速，保留切片内初始/末尾空闲；不是将十分钟压缩为两分钟。CPU Harness 使用真实 PBKDF2（固定 100000 次、seed=0），没有假装为 DataSciBench 计算或调用 DeepSeek。独立检查预期/实际 ID、每个开始和终态，以及计划纳秒间隔完全相同。

独立画图文件为 analysis/plot_windows.py。它读取已选窗口，分别生成三张统一纵轴、UTC 时间横轴的每 10 秒请求数论文图（英文、360 DPI PNG 和嵌入字体的矢量 PDF），不改变选窗或原始时间：

```bash
uv run python -m experiments.exp1.analysis.plot_windows \
  --traffic results/phase3/traffic-10min-v1 --output experiments/exp1/figure
```

分析支持显式 --window-minutes 10/15/60；例如用同一 arrivals.csv 加 --window-minutes 15 输出至新目录，再交给该画图脚本。

如需在本地探针完整跑一个已选十分钟窗口：

```bash
uv run python -m experiments.exp1.runner.validate_arrivals \
  --traffic results/phase3/traffic-10min-v1 --role high --duration-ms 600000 \
  --output results/phase3/high-full-10min
```

这条将实际发起请求十分钟，然后等待在途任务与积压完成；总运行时长可能超过十分钟。当前验收只运行固定切片，不能声称完成完整窗口服务实验。正式 Agent/Provider 运行可复用 `run_open_loop`，在实际 Harness 入口报告 started()；当前探针不生成 Agent 质量、SLO 或云费用结论。

验证记录见 [PHASE3_VALIDATION.md](PHASE3_VALIDATION.md)。单元测试中的 Event/sleep 仅用于协调控制流测试，均为 synthetic_test_fixture，不计入真实流量或 CPU 工作量。
