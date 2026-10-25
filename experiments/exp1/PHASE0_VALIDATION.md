# Phase 0 本地验收：MCP Session 语义纠正

- 主机与项目：`wlh-190:/home/wlh/Wabber`
- 日期：2026-10-25；Python 3.12.12；Node 22.4.0。
- 结果：Python **48/48**，VTS **4/4**，`git diff --check` 通过。
- 范围：仅本地执行，未构建/推送镜像，未部署云资源，也未做云端运行或账单验证。

## 撤销的误改

1. 撤销“MCP 跳过恢复就是缺陷”的判断。MCP Session 由部署环境隔离，普通调用直接访问原有驻留对象。
2. 撤销 MCP 的逐调用对象副本、快照恢复、默认对象重置、结束后提交及对象存储初始化。
   `session_scoped=True` 的代理现在只用于 FaaS，在 MCP 返回原对象。
3. 撤销 MCP 的文件锁、云锁和保护历史全局对象的跨调用兼容锁；只保留同一 Session 内实际需要的 domain 读写锁。
4. 撤销 MCP DataSci 每次启动 Python 并通过 JSON 重建执行状态的做法。改为每 Session 保留一个 worker 与完整 namespace。
5. 撤回先前把“单个 MCP 进程运行 16 个 UID”解释为生产 Session 隔离验证的结论；测试已改为 16 个独立 MCP 执行环境。

没有机械回退整份文件。旧 `__atsuite_state_snapshot` 的显式首次初始化接口保留，但运行中的 MCP Session 不能再次注入并重置对象。

## 保留的有效修复

- 统一 Tool Response、真实输出、MCP `isError`、JSON-RPC 错误及超时分类，保留旧客户端返回约定。
- 唯一 run/request ID、UTC 与单调时钟事件、JSONL、失败/超时输出及 traceback。
- FaaS 的恢复、持久化、调用隔离、原子快照覆盖和多实例条件写锁；r 不提交，stateless 不恢复/提交。
- 缺失指标不填零，统计覆盖率与费用估算标签。
- Notebook/Tensor 的只读声明。MCP 中直接使用它们的原对象；FaaS 中保留隔离代理。
- DataSci 不改变服务进程 cwd/stdout 的执行隔离与硬超时边界。

另外补齐 GCP MCP Dockerfile 的 `ATSUITE_RUNTIME=mcp`，避免该入口意外选择 FaaS 分支。仅修改配置，未构建镜像。

## 并发模型与测试证据

MCP handler 使用 `asyncio.to_thread()`，同一 Session 内可以有并行 Tool 调用，因此：

- 同一 domain 的 r 可并行；w/rw 与读取及其他写入互斥。
- 不冲突的 domain 可以并行；stateless 不申请状态锁。
- r 不做状态提交；正常 MCP 调用没有快照 I/O。
- 环境绑定一个 UID，其他 UID 误路由到该环境会报错，不模拟不同用户共享内存。

| 测试类型 | 实际执行方式 | 通过的检查 | 证据边界 |
| --- | --- | --- | --- |
| `MCPSameProcessAccessTests` | 一个 Runtime 内的线程，代表同一个 Session | 原对象身份/动态属性持续保留；不调用恢复/提交/重置/deepcopy/flock；16 路并行读取；冲突写入等待；stateless 及独立 domain 可并行；首次初始化不反复应用 | 单 Session 锁与 SDK 分支的单元测试，不作为生产隔离证据 |
| `MCPSeparateEnvironmentTests` | 本地 16 个独立 MCP 服务进程、16 个独立 worker、16 个 UID 和 Workspace，真实 HTTP 请求 | 每 Session 多次调用的变量、函数、自定义对象、RNG、文件句柄、结果连续；不同 Session 无污染；同 Session 16 次写入得到完整 1..16，16 次并行读取保持同一对象身份 | 真实本地进程环境隔离；不是云平台隔离验收 |
| FaaS 同进程测试 | 同一个真实 HTTP 服务处理 16 个 UID | 状态、文件与输出隔离；恢复后结果连续；只读不提交；失败不提交快照 | 本地 FaaS 执行入口 |
| FaaS 多实例测试 | 两个不同实例目录，共享有条件创建/原子覆盖的本地存储模型 | 16 次冲突递增无丢失，两个实例均恢复到最终 16 | 本地对象存储模型，不是云存储验收 |

## DataSci 改造依据与连续性

进程级 `datasci_workspace` 在隔离的 MCP 环境中属于单个 Session，本身不是跨用户共享缺陷。
MCP 现在保留原 Workspace 对象和驻留 worker；后续调用不重新导入/还原整个 namespace。
子进程的目的改为隔离服务进程的 cwd/stdout 并提供可终止的计算边界，而非模拟用户隔离。
FaaS 仍按调用启动子进程，并恢复/导出既有可序列化状态。

实际回归验证了：

- 同一 MCP worker 的 PID、Python 对象身份和工作目录在连续调用间保持一致。
- 自定义函数/类、RNG 进度、打开的文件句柄和中间文件可以继续使用；这些没有经过逐调用 JSON 重建。
- `os.write(1, ...)` 和 `os.write(2, ...)` 的原生输出完整进入本次 Tool 返回值。
- Tool 中主动 `os.chdir()` 后，下次调用保留新目录；MCP 服务进程目录保持不变。
- 普通 Python 异常保留已有 namespace，之后仍能使用原对象和函数，错误及输出正常返回。
- 硬超时返回 `timeout` 及超时前输出，停止 worker；后续调用报错要求新建 Session，不悄悄启动空 namespace。

SDK 的读写锁不是任意 Python 对象的事务回滚机制。MCP 保留原生内存异常语义；FaaS 失败调用不提交持久化快照。

## 原始记录

本轮记录位于：

`/home/wlh/Wabber/results/phase0/mcp-correction-d054301328914d6a82b1c442b5121c7a/`

- `python-tests.log`、`vts-tests.log`：完整输出。
- `validation-summary.json`：实际检查汇总。
- `mcp-environments/isolation.json`：16 个独立服务 PID、worker PID、UID 与 namespace 身份。
- `mcp-environments/0/` 至 `15/`：各 Session 的服务器 JSONL 日志、文件和 worker 诊断输出。
- `transports/`：Response 兼容性、FaaS 16 UID、MCP 错误/超时验证的日志与工作目录。

独立 MCP 环境中未生成 `.snapshots` 或 `.locks`。同进程单元测试另用 spy 验证没有恢复/提交/重置调用。
先前 `4929ba49a00445d98d88829dd764b208` 的原始记录保留，但其中同进程多 UID 的 MCP 测试不再作为生产隔离模型或本轮验收依据。
