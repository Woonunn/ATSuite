# 预算受限的流量窗口与放大图

当前默认 **10 分钟**；15/60 分钟保留为可选对照。此次在既有整周数据上按预算约束比较时长，没有用 Provider 成本、SLO 或优势反转筛选窗口。固定的 10%/50%/90% 到达数排序规则、UTC 对齐、并列按时间排序、零窗口保留全部不变。

## 为什么用 10 分钟

| 时长 | 完整候选窗口数 | 低档到达数 | 典型到达数 | 高档到达数 | 低/典型/高抽样到达率（每秒） |
| --- | --- | --- | --- | --- | --- |
| 10 分钟 | 1008 | 53 | 191 | 478 | 0.0883 / 0.3183 / 0.7967 |
| 15 分钟 | 672 | 79 | 286 | 715 | 0.0878 / 0.3178 / 0.7944 |
| 60 分钟（历史） | 168 | 314 | 1140 | 2826 | 0.0872 / 0.3167 / 0.7850 |

10 分钟下典型/低档强度约 3.60 倍，高/典型约 2.50 倍，高/低约 9.02 倍；相对一小时方案，三档平均强度差异均小于 1.5%。15 分钟没有增加新的强度档位，因此优先采用更省暴露时间/请求数的 10 分钟作为预算受限首轮。

这只是到达强度可区分的证据，不是单个短窗口具有充分 SLO 尾部统计精度的证明，也不意味着覆盖了整小时内的所有变化。短窗口减少请求数/到达暴露，具体云账单仍取决于实际部署、固定开销与排空时间，未做费用模拟。

当前三个 10 分钟区间（UTC）为：

- low：2026-06-06 02:10–02:20，53 次。
- typical：2026-06-04 04:40–04:50，191 次。
- high：2026-06-05 07:30–07:40，478 次。

原始 arrivals.csv、清洗报告和一小时验证结果未改写。缩短的是选取的真实连续区间，**time_scale 仍为 1.0**，没有把一小时压缩成十分钟。清洗 policy SHA 与选窗 policy SHA 分开记录；旧 arrivals.csv 的小时分组注释经校验后映射为新窗口 ID，初始空闲、精确相对间隔和右端点排除规则保留。

## 独立放大图脚本

当前 `analysis/plot_windows.py` 分别生成 low/typical/high 三张独立论文图。横轴为原始 UTC 时间，纵轴为 Requests per 10 s；统一 0–20 纵轴以保持强度对比。使用英文标注、STIX 衬线字体、简洁网格，PNG 为 360 DPI，PDF 为矢量图并嵌入 TrueType 字体。10 秒只是计数分箱，不改变调度时间。

交付目录为 `experiments/exp1/figure/`：traffic_low_10min、traffic_typical_10min、traffic_high_10min，各有 .png 和 .pdf；另保留 window_bins.csv 和输入/输出 SHA 的 plot_manifest.json。原始 3×2 组合图保留为历史分析产物：

- 默认图：`results/phase3/zoom-10min-v1/windows_zoom.png`、`windows_zoom.pdf`
- 对照图：`results/phase3/zoom-15min-v1/`、`results/phase3/zoom-60min-v1/`
- 窗口与统计：`results/phase3/traffic-10min-v1/`、`traffic-15min-v1/`
- 比较及源码 SHA：`results/phase3/window-duration-comparison.json`

复现（已有输出保留，新运行应换目录名）：

```bash
uv run python -m experiments.exp1.analysis.traffic \
  --arrivals results/phase3/arrivals-v1/arrivals.csv \
  --window-minutes 10 --output results/phase3/traffic-10min-v1
uv run python -m experiments.exp1.analysis.plot_windows \
  --traffic results/phase3/traffic-10min-v1 --output experiments/exp1/figure
```

默认输出目录也是 experiments/exp1/figure；已存在的目录不会覆盖，重新绘图可通过 --output 指定新目录。

改为 --window-minutes 15 或 60 即得到相同固定规则下的对照；画图不重新选择窗口。

## 验证与边界

10 项相关回归测试通过，覆盖 10/15/60 分钟候选数、同一源数据不改写、清洗规则变更拒绝、旧小时标注到新窗口、10 分钟标注跨 15 分钟选窗、边界时刻归属及纳秒间隔保持。绘图对全部三个长度逐一核对分箱总数与窗口请求数一致，PNG 已检查坐标与文字可读性。

新增 10 分钟场景仍以每窗前 120 秒原速轻量切片在 wlh-190 断网 Docker 验证：low 13、typical 34、high 105，共 **152 个请求全部成功，提交缺失率和失败/拒绝率均为 0**。提交误差 P99 约 1.60 / 1.54 / 2.82 ms，最大约 2.89 ms。证据在 `results/phase3/timing-10min-v1/validation.json`，验证器逐 ID 核对原始计划间隔没有变化。这不是将整个十分钟压缩成两分钟，也没有声称跑完了完整十分钟的 Agent/云费用实验。

默认 10 分钟指到达窗口；最后一批请求与已有积压需要继续执行到完成，总墙钟时间可能超过十分钟。原有 Aliyun 部署改动不属于本次窗口/绘图修改范围。

独立论文图已逐一检查：每张 60 个十秒分箱，总数为 53 / 191 / 478；三个 PDF 的 STIX Regular/Italic 均嵌入为 CID TrueType，没有 Type 3 字体。

新增横向三联图：experiments/exp1/figure/traffic_windows_10min_combined.png 和 .pdf。总标题为 Sampled Copilot Arrivals Across 10-Minute Traffic Windows，复用原三张图的配色、分箱、UTC 时间轴和公共纵轴；原独立图文件保持不变。analysis/plot_windows.py 会同时导出独立图及三联图。
