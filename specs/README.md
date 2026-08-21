# 美团销售智能助手 —— 设计文档索引

生产级多实例销售智能助手后端。提供销售/产品知识检索问答、话术推荐、业务数据查询、商家分析、沟通意图分析与拜访规划。

## 技术基线

MySQL 8（事务 + LangGraph Checkpoint + Outbox）· Milvus 2.5（向量 + 原生 BM25）· Kafka（异步事件）· Redis（租约 + SSE + 缓存 + 限流）· LangGraph 编排 · 配置走 env。

## 文档地图

| 文档 | 内容 |
| --- | --- |
| [adr/](./adr/README.md) | 6 份架构决策记录（MySQL / Milvus / Kafka / LangGraph-MySQL / env 配置 / 本地全栈） |
| [spec.md](./spec.md) | 需求规格：能力范围、FR/NFR、SLO、验收指标 |
| [architecture.md](./architecture.md) | 总体架构、服务边界、并发控制、Outbox、降级矩阵、工程结构 |
| [data-model.md](./data-model.md) | MySQL DDL、Milvus collection、Kafka topic、Redis key、Outbox/Inbox |
| [agent-design.md](./agent-design.md) | LangGraph 编排、Supervisor 路由、DAG、6 类 Worker、人格隔离、MySQL Checkpointer |
| [rag-design.md](./rag-design.md) | 文档切分、双路召回、RRF、Rerank、HyDE、反思、证据门禁 |
| [memory-design.md](./memory-design.md) | 两层记忆、滑动窗口 + 异步摘要、压缩、Skills |
| [knowledge-loop.md](./knowledge-loop.md) | 回流 pipeline、Judge、Checker、审核发布 |
| [local-dev.md](./local-dev.md) | 本地全栈、Makefile、env 配置、测试分层 |
| [tasks.md](./tasks.md) | 里程碑 M0–M5 与任务清单 |
| [checklist.md](./checklist.md) | 评审与验收清单 |

## 阅读顺序建议

新成员：spec → architecture → data-model → 各专题（agent/rag/memory/knowledge-loop）→ local-dev。
评审：adr → spec → checklist。
