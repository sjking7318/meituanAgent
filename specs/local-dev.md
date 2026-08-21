# 本地开发指南

关联：[ADR-0006](./adr/ADR-0006-local-dev-stack.md) · [architecture.md](./architecture.md)

## 1. 前置要求

- Docker + docker-compose
- Python 3.12
- `uv`（依赖管理）
- 公司/外部 OpenAI 兼容模型网关的 base_url 与 api_key

## 2. 一键起全栈

```bash
make up        # 起 MySQL / Redis / Milvus(standalone+etcd+minio) / Redpanda
make migrate   # Alembic 迁移建表
make seed      # 灌 tenant / user / 知识库种子
make dev       # 起 API（本地）
# 或
make worker    # 起异步 worker（ingestion/memory/distillation/eval）
```

## 3. docker-compose 组件

| 服务 | 镜像 | 端口 | 用途 |
| --- | --- | --- | --- |
| mysql | `mysql:8.4` | 3306 | 事务/Checkpoint/Outbox |
| redis | `redis:7` | 6379 | 租约/SSE/缓存/限流 |
| milvus | `milvusdb/milvus:v2.5.x` | 19530 | 向量 + BM25 |
| etcd | `quay.io/coreos/etcd` | — | Milvus 元数据 |
| minio | `minio/minio` | 9000/9001 | Milvus 存储 + 文档原文 |
| redpanda | `redpandadata/redpanda` | 9092 | Kafka 协议事件总线 |

精简 profile（仅核心并发开发）：`make up-core` 只起 MySQL + Redis。

## 4. 配置（env 单层，ADR-0005）

`.env`（从 `.env.example` 复制）关键项：

```dotenv
APP_ENV=local
LOG_LEVEL=INFO

# 基础设施
DATABASE_URL=mysql+asyncmy://sales:sales@localhost:3306/sales_assistant
REDIS_URL=redis://localhost:6379/0
MILVUS_URI=http://localhost:19530
KAFKA_BOOTSTRAP=localhost:9092
OBJECT_STORE_ENDPOINT=http://localhost:9000

# 鉴权（本地可 disabled）
AUTH_MODE=disabled

# 模型：本地直连外部网关（ADR-0006）
MODEL_PROVIDER=openai_compatible
MODEL_BASE_URL=https://your-gateway/v1
MODEL_API_KEY=sk-xxx
MODEL_NAME_ROUTER=fast-model
MODEL_NAME_SYNTH=reasoning-model
EMBEDDING_MODEL=bge-embedding
RERANK_MODEL=bge-reranker

# 检索参数
RRF_K=60
RRF_WEIGHT_DENSE=1.0
RRF_WEIGHT_BM25=1.0
RETRIEVAL_TOP_K=50
RERANK_INPUT=40
FINAL_EVIDENCE=8

# 预算
MAX_MODEL_CALLS=8
MAX_PLANNER_TASKS=4
MAX_REFLECTIONS=1
WALL_CLOCK_SECONDS=25

# Feature Flag
FEATURE_HYDE=true
FEATURE_REFLECTION=true
FEATURE_KNOWLEDGE_LOOP=true
```

生产环境用 `AUTH_MODE=oidc`，`MODEL_PROVIDER` 必须非 mock（Settings 校验强制）。密钥经 Secret Manager 注入，不入代码/镜像/日志。

## 5. Makefile 目标

| 目标 | 作用 |
| --- | --- |
| `make up` / `make down` | 起/停全栈 |
| `make up-core` | 仅 MySQL + Redis |
| `make migrate` / `make downgrade` | Alembic 迁移 |
| `make seed` | 种子数据 |
| `make dev` | 起 API（reload） |
| `make worker` | 起异步 worker |
| `make test` | 单元测试（不依赖外部服务） |
| `make test-int` | 集成测试（依赖 compose 栈） |
| `make lint` / `make type` | Ruff / mypy |
| `make fmt` | 格式化 |

## 6. 测试分层

| 层 | 依赖 | 命令 |
| --- | --- | --- |
| 单元 | 无（全 Mock，含模型 Mock Provider） | `make test` |
| 集成 | compose 起的 MySQL/Redis/Milvus/Kafka | `make test-int` |
| 契约 | 模型网关/业务 API/Tool Schema | `pytest tests/contract` |
| E2E | 全栈 + Mock 模型或真实网关 | `pytest tests/e2e` |

CI 只跑单元 + lint + type + 迁移检查 + 安全扫描（不依赖网络与外部模型）。

## 7. 链路追踪（Langfuse，可选）

自托管 Langfuse v2（仅需 Postgres），通过 compose `tracing` profile 起：

```bash
docker compose --profile tracing up -d langfuse-db langfuse
```

- 控制台 http://localhost:3000，账号 `admin@example.com` / `langfuse-local-admin`
  （由 compose 的 `LANGFUSE_INIT_*` 环境变量预置，含固定 API key）。
- 应用侧安装追踪依赖：`uv pip install -e '.[tracing]'`。
- 在 `.env` 设 `TRACING_ENABLED=true` 并重启应用；每次会话请求都会形成一条
  `sales-assistant-run` trace：主图（LangGraph）→ 各 Worker 节点（supervisor /
  retrieve / knowledge_qa / chitchat / clarify / synthesize）→ LLM 调用逐层嵌套为
  子 span。`session_id` 对应 conversation，便于按会话回看。
- 未起 Langfuse 或缺少 key 时自动降级为 no-op，不影响主流程。

## 8. 知识库治理

- 查看某文档版本的分片：`GET /v1/knowledge/document-versions/{version_id}/chunks`，
  返回每个 child chunk 的 `chunk_id`（形如 `…:p0:c0`）、`parent_id`、正文、标题与
  章节路径（`section_path`）。前端「知识库」页可点文档 → 展开版本 → 查看分片。
- 引用（citation）现在携带文档标题，回答里的 `[n]` 会映射到「标题 · 章节路径」。

## 9. 常见问题

- **Milvus 起不来**：确认 etcd + minio 健康；Milvus standalone 依赖两者。
- **首次拉镜像慢**：Milvus/Redpanda 镜像较大，预留时间。
- **模型直连失败**：检查 `MODEL_BASE_URL`/`MODEL_API_KEY`；单测用 Mock 不受影响。
- **迁移失败**：确认 MySQL 字符集 utf8mb4、UUID 列为 BINARY(16)。
