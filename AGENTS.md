# MCP Session
MCP Session 内部会持续保留状态，因此无需在每次执行 Tool 时重新恢复状态。FaaS 部署形式在每次 Tool 执行时都需要从对象存储中恢复状态，Tool 执行结束后需要将最新状态同步到对象存储。
