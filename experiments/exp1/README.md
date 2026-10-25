# Phase 0：实验基础

当前只实现和验证 Phase 0；尚未开始 LangGraph、DeepSeek、真实任务准备、到达时序或正式 E1 实验。
本地隔离测试的数值输入用于检查基础设施正确性，不代表真实 DataSciBench 工作负载。

## Tool Response 和执行记录

`RuntimeAdapter.invoke()` 返回 `InvocationResult`（SDK 中的 `ToolResponse`），保留真实输出类型，以及
`status`、`error`、`request_id`、`run_id`、`provider_request_id`、`client_elapsed_ms`、
`execution_elapsed_ms` 和 `provider_metadata`。无法测得的服务端耗时为 `null`。
MCP `isError` 和 JSON-RPC 错误按错误返回，超时保持 `timeout`。
旧 FaaS `invoke()` 的 `(request_id, is_stateful)` 和旧 MCP `invoke()` 的 request ID 返回约定保留；完整响应使用 `invoke_result()`。

Replay 使用唯一 run/request ID；JSONL 事件包含 UTC 时间、单调时钟、输出、错误和 traceback。
每次 Replay 的结果文件在成功或失败时均保存，不替换服务进程 stdout。
缺失指标使用 `null`；Excel 保留空值并单列 coverage。费用公式仅标为基于观测的估算，不当作账单。

## MCP：Session 内存驻留

已确认的部署前提是：每个 MCP Session 拥有独立执行环境，Session 生命周期内保留内存。
`ATSUITE_RUNTIME=mcp` 必须在 SDK 导入前设置，各 MCP Dockerfile 均声明此值；本轮不构建或推送镜像。
`ATSUITE_MCP_STATELESS` 只涉及 HTTP/MCP 传输，不改变应用 Session 的内存语义。

正常调用直接使用驻留的注册对象及稳定 Workspace：

- 不初始化对象存储，不读取/写入本地或云快照。
- 不逐调用复制、恢复、重置对象，也不在调用结束提交状态。
- 不使用跨 Session 文件锁、云锁或历史全局对象兼容锁。
- `session_scoped=True` 在 MCP 返回原对象；FaaS 才使用调用级代理。
- 一个执行环境绑定一个逻辑 UID；不同 UID 必须路由到各自环境，误路由会报错。

旧 `__atsuite_state_snapshot` 参数仍支持显式的首次初始化，以兼容现有 Invoker 的初始状态注入。
仅在首次有状态调用前应用一次；运行中的 Session 再次注入会报错，不恢复普通调用。

当前 MCP handler 使用 `asyncio.to_thread()`，允许同 Session 内 Tool 并行。因此在 SDK 内按状态域使用进程内读写锁：

| 模式 | 同一 Session、同一 domain 的并发规则 | 状态提交 |
| --- | --- | --- |
| r | 允许多个读取并行；与冲突写入互斥 | 无 |
| w / rw | 独占，序列化冲突写入 | 原地更新驻留状态，无快照提交 |
| stateless | 不申请状态锁 | 无 |

不同 domain 可以并行；Tool 必须正确声明实际访问的状态域及读写模式。
锁保证调度规则，不提供任意 Python 对象的事务回滚。普通 Python 异常不会自动重置 Session。

## FaaS：恢复与持久化

FaaS 保留逐调用对象副本及状态恢复，成功的 w/rw 调用提交快照；r 不提交，stateless 不恢复或提交。
稳定 Workspace 允许后续调用使用已保存的路径。失败调用不提交快照。

快照继续兼容 `service/uid.json`，因此同一快照里的写入保守地按 service + uid 串行化，防止不同 domain 合并时丢失更新。
同实例使用文件锁；多实例使用对象存储条件创建的写锁，提交采用原子覆盖，不先清空再追加。
云存储初始化失败时有状态调用明确报错，不退化为仅进程内保存。

云写锁不自动过期。写入进程异常终止可能留下 `service/uid.lock`；确认原写入已停止后才能清理。
`ATSUITE_STATE_LOCK_TIMEOUT` 默认 30 秒。
MCP 和 FaaS 的一致性指支持的数据与成功调用的可观察结果、文件连续性及访问规则，不要求相同存储机制或通用事务回滚。

## DataSci 执行方式及必要性

MCP 环境中的 `datasci_workspace` 保留原对象，不再把进程级对象视作跨用户共享缺陷。
数值计算仍在 ATSuite 托管 Tool 内完成，Agent Harness 不执行这些代码。

| 环境 | Python 执行方式 | 解决的问题 |
| --- | --- | --- |
| MCP | 每个 Session 保留一个 Python worker 与完整 namespace | 持续保留函数、自定义对象、RNG、文件句柄等原生内存状态；避免代码改变服务进程 cwd/stdout，并允许终止超时计算 |
| FaaS | 每次调用创建子进程，恢复/导出既有可序列化变量状态 | 允许同进程处理不同请求时隔离 cwd/stdout，并将成功状态持久化 |

MCP 后续调用不重新创建 worker，不通过 JSON 重建 namespace。仅首次可从显式初始状态建立 namespace。
输出通过独立 RPC fd 返回；stdout/stderr（包括原生库输出）只在 Tool 子进程中捕获，不重定向服务进程的流。
变量、工作目录及文件保持连续；普通异常保留 Session 的原生 Python 状态，并返回错误与已产生的输出。

`ATSUITE_DATASCI_TIMEOUT` 默认 240 秒。MCP 硬超时会终止 worker 并保留已产生输出；后续调用明确报错，必须新建 Session，不悄悄重新初始化内存。
FaaS 超时不提交持久化状态。数据与依赖应预先准备，Tool 不内置业务 API 调用。
此子进程用于执行和超时隔离，不是恶意代码安全沙箱。

## 本地测试及证据边界

在 `wlh-190:/home/wlh/Wabber` 执行：

```bash
ATSUITE_TEST_ARTIFACT_DIR=results/phase0/<唯一标识> \
  uv run --group aws --group gcp python -m unittest discover -s tests -q
~/.nvm/versions/node/v22.4.0/bin/node --test web/trace_viewer/metadata.test.mjs
```

测试清楚区分：

- `MCPSameProcessAccessTests`：模拟同一个 Session 的进程内锁、对象身份、无恢复/提交及首次初始化规则，不作为生产隔离证据。
- `MCPSeparateEnvironmentTests`：本地创建 16 个独立 MCP 服务进程，各自有独立 worker、Workspace、UID 和真实 HTTP endpoint，验证连续状态与不串状态；这是本地进程环境验证，不冒充云平台隔离验证。
- FaaS：同一真实 HTTP 服务的 16 个 UID，以及独立实例目录与共享条件存储模型下的写入冲突测试。后者是本地存储模型，不是云存储验收。

结果与撤销/保留清单见 `PHASE0_VALIDATION.md`。本轮只在 wlh-190 测试，不部署云资源，不采集账单。
