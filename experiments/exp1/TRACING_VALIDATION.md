# TracingAdapter 验证（2026-10-28）

本轮在当前机器真实执行 DataSciBench `csv_excel_34`，包含清洗、Gini 与 Lorenz 两路并行分析。
真实新 Capture 使用 `.env` 中的 DeepSeek 凭据、官方 API 和原有 Agent；没有用模型/Tool double 替代下面四次运行。
Tool 使用本地真实 HTTP/MCP RPC 和现有 NativeKernel（Landlock + nested seccomp），不是云部署。

| 运行 | LLM / Tool / Workflow 次数 | S_G (bytes) | D_G (s) | rho (KB/s) |
| --- | --- | ---: | ---: | ---: |
| 新 DeepSeek Capture，FaaS | 2 / 7 / 10 | 25,261,617 | 8.342197 | 3028.173 |
| 仓库历史 Capture 的 FaaS Replay | 2 / 7 / 10 | 25,264,113 | 8.119718 | 3111.452 |
| 新 Capture 的并发 FaaS Replay | 2 / 7 / 10 | 25,261,617 | 8.342197 | 3028.173 |
| 同时运行的 MCP Replay | 2 / 7 / 10 | 5,201,920 | 8.342197 | 623.567 |

每次结果均为 complete；Trace diagnostics 为空，调用实例 ID/请求/Session 相互独立。
Replay 严格校验原模型输入、Tool 参数、状态签名及产物签名，没有放宽 PNG 或数值校验。
MCP 的 SDK canonical load/save 字节均为 0；其非零 S_G 来自实际 branch bundle 通信。
FaaS 的重复 snapshot 读取逐次计数。这里的状态包含 SDK snapshot 和 branch 中声明的产物；
计量边界详见 [Tracing README](../../atsuite/tracing/README.md)。
这些数值只证明接入与测量正确，未匹配 FaaS/MCP 资源预算，不是策略性能比较。

本地原始证据（`results/` 按仓库规则不提交）：

- `results/tracing-validation-v3/capture/`：真实 DeepSeek 请求/响应、事件、结果、统一 Trace、ATG。
- `results/tracing-validation-v3/replay/`：原有仓库历史 Capture 重放。
- `results/tracing-concurrent/{faas,mcp}/`：同一新 Capture 在两个真实 Runtime 中并发重放。
- `results/tracing-validation-report.json`：对上述四次运行的断言检查与统计。
- `results/tracing-final/replay/`：最终代码追加关联字段和结构统计后的真实重放。

最终代码真实重放还验证了显式 Runtime call_id/Session UID 关联、merge worker 资源字段、
10 条 Workflow 依赖边、观测并行峰值 2。

另外检查了：D_G 与原事件中的 LLM 等待间隔之和一致；S_G 与每个 Tool 的实测读写之和一致；
两路 ANALYSIS 的前驱是同一个 DISPATCH，彼此没有依赖；MERGE 保留两个前驱；
导出的 ATG 无环且可被既有 `Trace.from_file` 读取。

## 环境和复现

Harness 固定 LangGraph 1.0.5、langchain-core 1.2.6、Python 3.12.3；本轮 OpenAI SDK 为 2.14.0。
Tool 使用 `nodes/AgentTools/requirements.txt` 中的固定版本（包括 numpy 1.26.4、pandas 2.3.3、
scikit-learn 1.3.2、scipy 1.17.1、matplotlib 3.11.0）。没有改写项目原有虚拟环境。
初次运行由于缺少 sklearn/kernel 导入路径而失败，错误 Trace 已保留；补齐环境后通过上述运行。

日常复现按 [Agent README](../../benchmarks/DataSciBench/langgraph_agent/README.md) 的固定 Docker 环境执行原 Capture/Replay 命令。
本轮因本机缺少原参考镜像，使用以下临时环境复现已有 NativeKernel 路径：

```bash
uv sync --locked
uv venv /tmp/wabber-tracing-env --python .venv/bin/python
uv pip install --python /tmp/wabber-tracing-env/bin/python \
  -r benchmarks/DataSciBench/nodes/AgentTools/requirements.txt cloudpickle==3.1.2
# 临时环境复用项目 Harness；kernel 模块在镜像中原本是平铺安装的。
printf '%s\n' "$PWD/.venv/lib/python3.12/site-packages" \
  "$PWD/benchmarks/DataSciBench/nodes/DataSci" \
  > /tmp/wabber-tracing-env/lib/python3.12/site-packages/wabber.pth
```

随后使用独立输出目录执行：

```bash
WABBER_EXECUTION_BACKEND=native /tmp/wabber-tracing-env/bin/python - <<'PY'
import asyncio, json, uuid
from pathlib import Path
from dotenv import load_dotenv
from experiments.exp1.capture.capture_agent_traces import run
from experiments.exp1.tests.check_agent_lifecycle import service

load_dotenv('.env', override=False)
root = Path('results') / ('tracing-' + uuid.uuid4().hex)
config = json.loads(Path('benchmarks/DataSciBench/config/langgraph_experiment.json').read_text())
with service('faas', root / 'service', 'unused-native') as tool:
    config['tool'] = {**tool, 'execution_scope': 'local native tracing validation'}
    asyncio.run(run('csv_excel_34', config, root / 'capture'))
    asyncio.run(run('csv_excel_34', config, root / 'replay', root / 'capture'))
PY
```

输入数据仍须通过原准备流程校验哈希；新 Capture 的模型决策和耗时会随真实 API 改变。
并发复现时每请求使用独立 Collector/目录；MCP 每个请求启动独立 service 进程。

## 自动检查与限制

`tests/test_tracing.py` 使用真实 StateGraph，覆盖原生 LLM/Tool 回调、动态路由及映射、循环、
12 路 Send/Command 的实际发送者、长短分支 barrier、同图并发 Session、错误持久化、
SDK 桥接重试/关联字段、实际文件 I/O 字节、失败/旧 Runtime 的缺失值和 rho 公式。
LLM/Tool doubles 只用于这些控制流测试。

全量单测首次检查暴露三项环境失败：`test_e1b` 两项缺少未准备的
`benchmarks/DataSciBench/data/human_15/sales.csv`；16 个 MCP 进程并发测试在 reader barrier 处超时。
后者已在未修改的基线提交 `d87571e` 的临时 worktree 独立复现（31.6 秒，同一失败），不是本轮引入的回归。
本轮未修改这些实验输入或并发调度逻辑。最终排除这三项已确认的环境失败后，**其余 137 项测试全部通过**；
9 项新增 tracing 测试全部通过，Ruff（E9/F/I）、`git diff --check` 和 `uv lock --check` 通过。

以上为 2026-10-28 首轮范围；嵌套子图与 checkpoint resume 已在下述 2026-10-29 续轮中补齐。
云 Provider 部署和 deferred 节点仍未验证。


## 2026-10-29：嵌套子图与 checkpoint 续跑

新增 `tests/test_tracing_continuation.py`，使用固定版本的真实 LangGraph/checkpointer 验证：

- 多层同名子图、两路并行子图与 Send/条件路由隔离，以及 `Command.PARENT` 返回父图。
- 包装函数在调用子图前后的模型调用保持真实顺序；未知动态子图会保留不完整诊断。
- 原生 `interrupt()` 多次暂停、`interrupt_before/after` 静态断点、异常后 `None` 重试。
- 在独立子进程中恢复测试 checkpoint 和 `TraceCollector.load`；不中断业务图的原生执行语义。
- 一个并行分支失败后，成功分支的 pending write 被复用，只有失败分支重新调用模型。
- 嵌套中断后从保存的 Trace 关联旧子节点和新的父节点尝试，导出的包含/依赖边仍构成无环图。
- 错误 thread/旧 checkpoint/同一 checkpoint 内未经采集的 resume 写入会被拒绝。
- checkpoint 观测读取失败时保留原业务异常，并写出不完整诊断；未知存储字节不会被补零。
- 执行前持久化 in-progress 标记；未完整收尾的 Trace 即使 checkpoint ID 未变，也不发布完整历史统计。

真实 DataSciBench 验证使用原 `csv_excel_34` Capture 中的模型决策，运行真实 Tool RPC 和 NativeKernel。
把原 `build_graph(...).builder.compile(interrupt_before=['MERGE'])` 作为父图的 `agent` 子图，
父图使用 `InMemorySaver`；每次暂停均通过 `TraceCollector.load` / 新 Adapter 继续，保持原 Tool Session。
在两次 MERGE 前暂停，最后通过原 Replay 的全部模型输入、Tool 参数、状态与产物一致性检查。
本轮没有新增模型 API 请求；模型等待使用此前真实 Capture 的间隔，Tool 均重新计算。

本地证据：`results/tracing-nested-resume/7a79a8cd22c245499253e0a4c51015a6/trace/`。
结果为 complete，diagnostics 为空：

| 指标 | 结果 |
| --- | ---: |
| 执行段 / 暂停次数 | 3 / 2 |
| LLM / Tool 实际调用数 | 2 / 7 |
| Workflow 执行实例（含父节点重跑） | 13 |
| Workflow 依赖边 / 叶节点并发峰值 | 12 / 2 |
| D_G | 8.342196825 秒 |
| 已观测 Tool 状态传输 | 25,261,617 bytes |
| 完整 S_G / rho | null / null |

完整 S_G/rho 为 null 是因为本轮没有测量 checkpointer 存储 I/O；不会将其假定为零。
LLM/Tool 调用数与原完整执行相同，表明已完成分析分支没有因恢复而重复计数。
跨进程测试中的 checkpoint 序列化只用于测试自身创建的可信 fixture，产品代码只加载 Trace JSON，
业务 checkpoint 的存储与恢复仍由应用负责。

最终回归：新增 14 项 continuation 测试通过；沿用首轮确认的三项环境失败排除清单后，
其余 **151 项测试全部通过**（33.9 秒）。Ruff E9/F/I、格式检查、`git diff --check` 与 `uv lock --check` 通过。
