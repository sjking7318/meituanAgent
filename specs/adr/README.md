# 架构决策记录（ADR）

本目录记录美团销售智能助手后端的关键架构决策。每条决策独立成文，包含背景、可选项、结论与后果。决策一经接受即为团队共识；如需变更，新增一条 ADR 覆盖旧决策，不原地删改历史。

## 决策索引

| 编号 | 标题 | 状态 |
| --- | --- | --- |
| [ADR-0001](./ADR-0001-datastore-mysql.md) | 事务存储选用 MySQL 8 | 已接受 |
| [ADR-0002](./ADR-0002-vector-milvus.md) | 向量检索选用 Milvus，BM25 用 Milvus 原生稀疏检索 | 已接受 |
| [ADR-0003](./ADR-0003-event-bus-kafka.md) | 异步事件总线选用 Kafka + MySQL Outbox | 已接受 |
| [ADR-0004](./ADR-0004-langgraph-mysql-checkpoint.md) | Agent 编排用 LangGraph，Checkpoint 落 MySQL | 已接受 |
| [ADR-0005](./ADR-0005-dynamic-config.md) | 配置统一收敛到环境变量层 | 已接受 |
| [ADR-0006](./ADR-0006-local-dev-stack.md) | 本地开发全栈与外部模型直连 | 已接受 |

## 状态定义

- **提议中**：正在讨论，未定稿。
- **已接受**：团队共识，作为当前基线。
- **已废弃**：被后续 ADR 取代，保留供追溯。
