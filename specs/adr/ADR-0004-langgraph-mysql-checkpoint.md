# ADR-0004 Agent 编排用 LangGraph，Checkpoint 落 MySQL

| 属性 | 内容 |
| --- | --- |
| 状态 | 已接受 |
| 决策日期 | 2026-08-11 |
| 影响范围 | Agent Runtime、Supervisor-Worker、Planner、跨实例恢复 |

## 背景

Supervisor-Worker 多 Agent 需要可检查点、可恢复、可观测的编排框架。系统是多实例无状态部署，任一实例必须能接续中断的 Run；执行状态不能只存在进程内存。

## 可选项

1. **自研状态机**：完全可控，但要自己实现图执行、检查点、恢复，成本高。
2. **LangGraph**：图状态编排、原生 checkpoint 机制、社区活跃；但官方 checkpointer 只有 Postgres/Redis/SQLite，**无 MySQL**。
3. **其他编排框架（CrewAI/AutoGen 等）**：抽象层偏高，难以精细控制预算与人格隔离。

## 决策

采用 **LangGraph** 做运行时编排，但严格约束其边界：

1. **仅用于运行时编排与 checkpoint**，领域逻辑（路由规则、RRF、权限、预算、记忆治理）保持框架无关，放在 domain/application 层。
2. **自研 MySQL Checkpointer**：实现 LangGraph `BaseCheckpointSaver`（async），将 checkpoint、channel writes、metadata 持久化到 MySQL `lg_checkpoints` / `lg_checkpoint_writes` 表。
3. **RunState 用 TypedDict**，只存结构化字段（route/plan/task_results/evidence/budgets/versions）。Prompt 全文、大 Tool 结果、原文不进 checkpoint，只存版本号/摘要/对象引用。
4. 任一 Agent Runtime 实例可基于 `run_id` 从 MySQL 恢复 checkpoint 续跑，配合 fencing token 防止旧实例覆盖。

## 为什么 checkpoint 落 MySQL 而非 Redis

- 可靠性优先：checkpoint 是可恢复性的关键，Redis 驱逐/故障会丢状态。
- 与业务事务同库：可与 run 状态、消息在同一事务边界内一致提交。
- Redis 仍承载 Lease、SSE、缓存、限流等易失、高频、可重建的状态。

## 后果

- 正面：得到成熟图编排与恢复能力；状态可靠持久；领域逻辑不被框架绑架。
- 负面：需自研并维护 MySQL checkpointer；需跟进 LangGraph checkpoint 协议版本变化。
- 缓解：checkpointer 覆盖契约测试；固定 LangGraph 版本区间；抽象编排入口，必要时可替换为自研状态机。
