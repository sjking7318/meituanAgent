# HANDOFF — 美团销售智能助手后端

> 面向下一个接手的 AI/工程师的技术交接文档。目标：读完能独立跑起来、改得动、知道
> 坑在哪、知道下一步做什么。**请先读本文件，再读 `specs/`。**

---

## 0. TL;DR（30 秒）

- **是什么**：生产级多 Agent 销售智能助手后端。六边形架构 + LangGraph 编排 +
  Agentic RAG + 会话记忆 + 全链路追踪。
- **技术栈**：Python 3.12 / FastAPI / LangGraph / MySQL 8.4 / Milvus 2.5 /
  Redis 7 / Redpanda(Kafka) / 阿里 DashScope（LLM+Embedding+Rerank） / Langfuse(可选追踪)。
- **怎么跑**：`make up` 起中间件 → `make migrate` → 填 `.env` 的 key → `make dev` →
  打开 http://localhost:8000 （自带 Web UI）。
- **验证**：`make check`（lint+type+单测）+ `uv run python scripts/e2e_check.py`（真机端到端）。
- **状态**：M0–M3 + 记忆(M4 一部分) + 追踪 + 知识库治理**已完成并真机验证**；
  M4 剩余(Planner/HyDE/反思/长期记忆) 与 M5(知识回流/生产化) **未做**。详见 §6。

---

## 1. ⚠️ 上传 GitHub 前的安全须知（务必先看）

- **真实 API Key 只在 `.env` 里，`.env` 已被 `.gitignore` 忽略**，不会进仓库。提交前
  再确认一次：`git status` 不应出现 `.env`。
- **本会话历史里明文出现过一个真实的 DashScope key**（`sk-09f99b...`）。强烈建议去
  阿里云 DashScope 控制台**吊销并重建**一个新 key，然后只写进本地 `.env`。这是最稳妥的做法。
- `docker-compose.yml` 里的 `sk-lf-sales-assistant-local` **不是真实凭证**，是本地
  Langfuse 的自造 dev key，可安全公开。
- `.env.example` 是给别人复制的模板，所有 key 字段都留空——**永远不要把真实值写进它**。

---

## 2. 架构总览（先建立心智模型）

**六边形架构（Ports & Adapters）**，依赖只能由外向内指：

```
api (FastAPI 路由/鉴权)
        │  调用
application (编排：会话/检索/摄入/记忆 service)   ← 用例层，无框架依赖
        │  依赖
domain (entities + ports<Protocol> + errors)      ← 纯业务，零第三方框架
        ↑  实现 ports
infrastructure (mysql / milvus / redis / model_gateway / observability)  ← 适配器
```

铁律：**`domain/` 不许 import FastAPI / SQLAlchemy / LangGraph / pymilvus / 任何 SDK**。
所有外部能力都以 `domain/ports.py` 里的 `Protocol` 表达，infrastructure 提供实现，
`main.py` 的 `build_container()` 做依赖注入组装。

四大能力：
1. **Agentic RAG**：Milvus 双路召回（dense HNSW + 原生 BM25）→ 加权 RRF 融合 →
   DashScope rerank → 证据打包 → **证据门禁（无证据拒答）** → 带编号引用 `[1][2]`。
2. **Supervisor-Worker 多 Agent**：意图分类路由到 knowledge_qa / chitchat / clarify，
   人格隔离（分析师人格只活在 supervisor 节点）。
3. **会话记忆**：滑动窗口最近 N 轮 + 版本化滚动摘要（MySQL，跨实例一致）。
4. **历史回流 pipeline**：**尚未实现**（见 §6）。

---

## 3. 代码地图（改代码前照这张表定位）

### domain（纯业务，改这里要想清楚契约）
- `domain/entities.py` — 所有实体：`Conversation/Message/AgentRun/Evidence/ChunkRecord/`
  `ChunkView/ConversationSummary/ModelTurn/...`。UUID 用 UUIDv7（`new_id()`）。
- `domain/ports.py` — 所有端口 Protocol：`ModelGateway/Embedder/Reranker/Retriever/`
  `KnowledgeIndexer/LeaseManager/RunEventStream/*Repository/UnitOfWork/`
  `ConversationSummaryRepository`。
- `domain/errors.py` — 领域异常（`DomainError` 基类 → HTTP 映射见 `api/errors.py`）。

### application（用例编排）
- `conversation_service.py` — **核心**。`send_message()` 做：Redis 租约 → 幂等检查 →
  写 user 消息 → 跑 agent → fencing/CAS 校验 → 写 assistant 消息 → 触发摘要。SSE 事件
  用 `_emit()`（事件类型：run.started/replayed/failed/conflicted、message.completed 等）。
- `retrieval_service.py` — RAG：embed query → 双路召回 → `reciprocal_rank_fusion()` →
  rerank → 证据打包。
- `ingestion_service.py` — 文档摄入：parse → chunk → embed（批） → 写 Milvus →
  标记 published。含 `list_chunks()`（知识库治理）。
- `memory_service.py` — 记忆：`load_context()`（摘要 system turn + 最近原始轮）、
  `maybe_summarize()`（超阈值内联生成新版本摘要，失败静默不阻塞主流程）。

### agents/runtime（LangGraph）
- `graph.py` — **AgentRuntime**：StateGraph 定义 + 节点（`_supervise/_route/_classify_intent/`
  `_retrieve/_knowledge_qa/_chitchat/_clarify/_synthesize`）。`run()` 里把 tracing
  callbacks 注入 `RunnableConfig`。
- `checkpointer.py` — 自研 `MySQLCheckpointSaver`（LangGraph checkpoint 落 MySQL，
  任意实例可按 run_id 恢复）。
- `state.py` — `RunState` TypedDict（含 `citations` 字段）。

### infrastructure（适配器）
- `mysql/models.py` — SQLAlchemy 表模型（UUID=BINARY(16)、时间=DATETIME(6) UTC、utf8mb4）。
- `mysql/repositories.py` — 仓储 + `SqlUnitOfWork`（含 `summaries`）。
- `milvus/schema.py` — collection schema（dense + sparse BM25 + 中文 analyzer + alias）。
- `milvus/indexer.py` — 写入/删除/`list_chunks`（治理查询用 `consistency_level="Strong"`）。
- `milvus/retriever.py` — 双路召回（lazy async client，绑定运行时 event loop）。
- `milvus/memory.py` — `InMemoryRetriever/InMemoryKnowledgeIndexer`（测试/Mock，无需真 Milvus）。
- `model_gateway/gateway.py` — `OpenAICompatibleModelGateway`（LLM）+ `MockModelGateway`。
- `model_gateway/embeddings.py` — `DashScopeEmbedder/DashScopeReranker`（**原生 API，非 OpenAI 兼容**）+ Mock。
- `redis/lease.py` — 租约 + fencing token；`redis/event_stream.py` — SSE 事件流。
- `observability/tracing.py` — Langfuse callback 工厂（可开关，缺 SDK/key 自动 no-op）。
- `skills/library.py` — `FileSystemSkillLibrary`（渐进式披露：catalog/load/read_resource）；
  `skills/__init__.py` — `build_skill_library()` 工厂。SkillLibrary 端口在 `domain/ports.py`，
  技能内容目录在**仓库根 `skills/`**（不是这个 python 包）。

### 其它
- `main.py` — `build_container()` 依赖注入 + `create_app()`（挂载 `web/` 静态页 + CORS）。
- `settings/__init__.py` — Pydantic Settings，单层 env（启动冻结，无热更新）。
- `ingestion/chunker.py` — Parent-Child 切分（`chunk_id` 形如 `<docver>:p0:c0`）。
- `web/index.html` — 单页 Web UI（对话 + 知识库管理 + 分片浏览），同源挂在 `/`。
- `scripts/e2e_check.py` — 真机端到端脚本（要求 app 已在 8000 跑起来）。
- `scripts/check_gateways.py` — 探测三个 DashScope 网关连通性。

---

## 4. 数据存储约定（踩坑高发区）

- **MySQL**：UUID 存 `BINARY(16)` 用 UUIDv7（时间有序、索引友好）；时间 `DATETIME(6)`
  存 UTC；字符集 utf8mb4；并发用乐观锁 CAS + `ON DUPLICATE KEY` upsert。
- **Milvus**：查询走 alias `knowledge_active`（**indexer 建 alias，retriever 查 alias**，
  两边必须一致，否则检索报 collection 不存在）。ACL/租户/状态/时效**标量过滤前置**（召回前）。
  `chunk_id` = Milvus 主键 `pk`；读回时 `_OUTPUT_FIELDS` 必须含 `FIELD_PK` 否则 chunk_id 为空。
- **迁移**：`migrations/versions/`，最新到 `20260820_0005`（conversation_summaries）。
  改模型后 `uv run alembic revision --autogenerate` + 人工核对，再 `make migrate`。

---

## 5. 本地运行 & 验证

```bash
# 1) 依赖
uv sync --all-extras                 # 或 make install

# 2) 起中间件（MySQL/Redis/Milvus/etcd/minio/redpanda）
make up                              # 首次拉 Milvus/Redpanda 镜像较慢

# 3) 迁移
make migrate

# 4) 配置：复制模板并填 DashScope key
cp .env.example .env                 # 然后填 LLM_API_KEY / EMBEDDING_API_KEY
#   若用真实模型：MODEL_PROVIDER=openai_compatible, EMBEDDING_PROVIDER=dashscope
#   本地/CI 无 key：MODEL_PROVIDER=mock, EMBEDDING_PROVIDER=mock

# 5) 起服务（自带 Web UI）
make dev                             # http://localhost:8000

# 6) 质量门禁
make check                           # ruff format+lint / mypy / 单测(+覆盖率)
make test-int                        # 集成测试（需中间件在跑）

# 7) 真机端到端（app 已在 8000 跑）
uv run python scripts/check_gateways.py   # 先确认三网关连通
uv run python scripts/e2e_check.py        # ingest→分片→引用→三路由→记忆，应 PASS
```

**测试分层**：单测 `-m "not integration"` 全 Mock、不碰网络（CI 只跑这层）；
集成测试 `-m integration` 需要真 MySQL/Milvus。当前规模：单测 62 passed、集成 5 passed。

### 链路追踪（Langfuse，可选）
```bash
docker compose --profile tracing up -d langfuse-db langfuse   # 起 Langfuse v2 + Postgres
uv pip install -e '.[tracing]'                                # 装 langfuse + langchain
# .env 里：TRACING_ENABLED=true（key 用 compose 预置的 pk/sk-lf-sales-assistant-local）
make dev                                                      # 重启应用
```
控制台 http://localhost:3000（`admin@example.com` / `langfuse-local-admin`）。每次会话
形成一条 `sales-assistant-run` trace：主图 LangGraph → 各节点 → LLM 调用逐层嵌套为
子 span，`session_id` = conversation_id。未起 Langfuse 时自动降级 no-op。

---

## 6. 完成度 & 待办（下一个 AI 从这里接手）

### ✅ 已完成并验证
- M0 工程骨架 + docker-compose 全栈 + Alembic 迁移。
- M1 多实例会话（幂等/租约/fencing/CAS/SSE 恢复）+ MySQL LangGraph checkpointer +
  单 Worker 知识问答。
- M2 文档摄入（Parent-Child 切分）+ Milvus 双路召回 + RRF + DashScope rerank +
  证据门禁 + 编号引用 + 文档生命周期 API。
- M3 Supervisor 多 Agent 路由（knowledge_qa/chitchat/skill/clarify）+ 人格隔离。
- 会话记忆（滑动窗口 + 版本化滚动摘要，内联触发）。
- 全链路追踪（Langfuse，节点级 span 树）。
- 知识库治理：查看分片接口 + 前端分片浏览；引用带标题（修了 title 恒 None 的 bug）。
- **Skills（渐进式披露，Claude-Code 风格）**：`SkillLibrary` 端口 +
  `FileSystemSkillLibrary`（三级加载：level-1 catalog 只出 name+description 常驻上下文；
  level-2 `load()` 按需读 `SKILL.md` 正文；level-3 `read_resource()` 按需读引用文件，
  带路径穿越防护）。技能内容在仓库根 `skills/<name>/SKILL.md`（示例：visit-planning、
  visit-analysis）。Supervisor 注入 level-1 catalog 让模型选技能，命中则路由到 `skill`
  worker 加载正文作为操作指令执行。代码在 `infrastructure/skills/`。

### ❌ 未做（按优先级）
- **M4 剩余**：Agentic Planner（sub-question DAG）、HyDE、反思/Verifier、
  长期记忆（提取/分类/去重/冲突/检索）、上下文 token 预算裁剪。
  设计见 `specs/memory-design.md`、`specs/rag-design.md`、`specs/agent-design.md`。
- **其余业务 Worker**：Business Data / Merchant Analyst / Sales Coach / Intent Analyst
  等 Worker 及其外部只读 Tool Adapter 尚未实现。新增技能只需在 `skills/` 下加一个
  `<name>/SKILL.md`（frontmatter + 正文），无需改代码即被 catalog 发现。
- **记忆异步化**：当前摘要是**内联**触发（`memory_service.maybe_summarize`）。设计目标
  是 Kafka 事件 + Memory Worker 异步（`workers/main.py` 现在是空占位）。
- **M5 全部**：知识回流 pipeline（LLM-as-a-Judge）、评测 Golden Set、Outbox/Inbox +
  Kafka 幂等消费、OTel 指标/Dashboard、安全测试、Helm/灰度、Runbook。
- **OIDC 鉴权（T-102）**：`api/auth.py` 现在只有 disabled 模式（信任 header）；
  oidc 模式是占位，生产必须实现 JWT 校验。
- **文档解析**：`ingestion/parser.py` 目前主要处理 markdown/text；PDF/DOCX/PPTX/OCR 未做。

### 任务清单来源
`specs/tasks.md` 有全量任务（T-001~T-514，按 M0~M5 分里程碑）。`specs/checklist.md`
是发布门禁清单。ADR（架构决策记录）在 `specs/adr/`，改架构前先读对应 ADR。

---

## 7. 关键坑位（前人踩过，别再踩）

1. **DashScope 的 Embedding/Rerank 不是 OpenAI 兼容**：走原生端点
   （`/api/v1/services/embeddings/multimodal-embedding`、`.../rerank/text-rerank`），
   请求/响应结构是 `input.contents` / `output.results`。见 `embeddings.py`。LLM 才走
   `/compatible-mode/v1`（OpenAI 兼容）。
2. **免费额度会耗尽导致 403**：换个可用模型（qwen-max/plus/turbo）或换 key。
   先跑 `scripts/check_gateways.py` 定位是哪一路挂了。
3. **Milvus alias 一致性**：retriever 查 `knowledge_active` alias，indexer 负责建/校验它。
   两边不一致 → 检索报错。
4. **Milvus grpc channel 绑 event loop**：AsyncMilvusClient 必须在运行循环内 lazy 创建，
   不能在 container-build 时 eager 建（否则 "Future attached to a different loop"）。
5. **Milvus 写后立即读不到**：治理查询 `list_chunks` 用 `consistency_level="Strong"`；
   集成测试插入后要 `flush()` + sleep。
6. **pydantic-settings 2.14 下 `_env_file=None` 不可靠**：测试里要用显式 kwargs 覆盖
   （`llm_base_url=None` 等），不能靠禁用 .env。见 `tests/conftest.py`。
7. **Langfuse v2 callback 硬依赖 `langchain` 元包**（不只是 langchain-core），已加进
   `.[tracing]` extra。
8. **引用 title 曾恒为 None**：因为摄入没把文档标题下沉到 chunk。已修
   （`ingestion_service._process` 传 title → ChunkRecord → Milvus FIELD_TITLE）。

---

## 8. 约定 & 风格（保持一致）

- 提交前必须 `make check` 全绿（ruff format+lint / mypy strict / 单测）。
- 新增外部能力：先在 `domain/ports.py` 定 Protocol，再在 infrastructure 写实现，
  最后在 `main.py` 注入。不要让 domain 直接依赖 SDK。
- 新增配置项：加到 `settings/__init__.py` + `.env.example`（留空值），不要只加一处。
- 数据库变更：改 `models.py` → 生成迁移 → 人工核对 → `make migrate`。
- 注释/命名跟随现有风格（英文注释、领域术语一致）。

---

## 9. 文档索引

| 文件 | 内容 |
| --- | --- |
| `specs/spec.md` | 需求与范围 |
| `specs/architecture.md` | 总体架构 |
| `specs/data-model.md` | 数据模型（MySQL + Milvus） |
| `specs/agent-design.md` | 多 Agent 编排 |
| `specs/rag-design.md` | 检索链路 |
| `specs/memory-design.md` | 记忆工程 |
| `specs/knowledge-loop.md` | 知识回流 pipeline（未实现） |
| `specs/local-dev.md` | 本地开发（含追踪 + 治理章节） |
| `specs/tasks.md` | 全量任务清单 |
| `specs/checklist.md` | 发布门禁 |
| `specs/adr/` | 6 份架构决策记录 |
| `README.md` | 项目简介 |
