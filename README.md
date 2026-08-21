# 美团销售智能助手后端

生产级多实例销售智能助手后端：销售/产品知识检索问答、话术推荐、业务数据查询、商家分析、沟通意图分析与拜访规划。

## 技术栈

- **API**：FastAPI + Uvicorn（Async、SSE、OpenAPI）
- **事务存储 + LangGraph Checkpoint + Outbox**：MySQL 8（SQLAlchemy 2 Async + asyncmy + Alembic）
- **向量 + BM25 双路召回**：Milvus 2.5（dense HNSW + sparse 原生 BM25）
- **异步事件总线**：Kafka（本地 Redpanda）+ MySQL Outbox
- **租约 / SSE / 缓存 / 限流**：Redis
- **Agent 编排**：LangGraph + 自研 MySQL Checkpointer
- **配置**：环境变量单层（启动冻结，ADR-0005）

设计文档见 [specs/README.md](./specs/README.md)。架构决策见 [specs/adr/](./specs/adr/README.md)。

## 快速开始（本地）

```bash
make install      # uv sync 依赖
cp .env.example .env
make up           # 起全栈 MySQL/Redis/Milvus/Redpanda/MinIO
make migrate      # Alembic 建表
make seed         # 灌 demo tenant/user
make dev          # 起 API（http://localhost:8000）
```

仅核心并发开发：`make up-core`（只起 MySQL + Redis）。

## 质量门禁

```bash
make lint         # ruff format --check + ruff check
make typecheck    # mypy strict
make test         # 单元测试（不依赖外部服务）
make test-int     # 集成测试（需 make up）
make check        # lint + typecheck + test
```

当前状态：M0 工程基线 + M1 会话骨架已落地（多实例并发控制：幂等 / Redis 租约 + fencing / MySQL CAS / Redis Stream SSE 恢复 / LangGraph Mock 单 Worker 问答）。单元测试 30 项通过，覆盖率 78%。

## 里程碑

见 [specs/tasks.md](./specs/tasks.md)：M0 基线 → M1 会话 → M2 文档+检索 → M3 多 Agent+Tool → M4 Planner+记忆 → M5 回流+生产化。
