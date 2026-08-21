# 数据设计

关联：[architecture.md](./architecture.md) · [ADR-0001](./adr/ADR-0001-datastore-mysql.md) · [ADR-0002](./adr/ADR-0002-vector-milvus.md)

## 1. 存储职责划分

| 存储 | 职责 |
| --- | --- |
| MySQL 8 | 事务数据、LangGraph Checkpoint、Outbox/Inbox、审计 |
| Milvus 2.5 | 知识 chunk 向量 + BM25 稀疏向量、长期记忆向量 |
| Redis | 会话租约、SSE 短期事件流、缓存、限流计数 |
| Kafka | 异步事件（文档处理、摘要、回流、评测、审计） |
| S3/MinIO | 文档原文、解析产物、评测报告 |

## 2. MySQL 通用规范（ADR-0001）

- **UUID**：`BINARY(16)` 存储，应用层生成 **UUIDv7**（时间有序，索引友好）。
- **时间**：`DATETIME(6)`，写入 UTC，不依赖 DB 时区。
- **字符集**：`utf8mb4` / `utf8mb4_0900_ai_ci`。
- **引擎**：InnoDB。
- **JSON**：结构化 payload 用原生 `JSON` 类型。
- **乐观锁**：`version` 列 + `UPDATE ... WHERE version=?` 判 rowcount。
- **幂等**：唯一约束 + 捕获 `IntegrityError`。
- **迁移**：Alembic，遵循 expand/migrate/contract。

## 3. MySQL 核心表

### 3.1 租户与用户

```sql
CREATE TABLE tenants (
  id            BINARY(16)   NOT NULL,
  name          VARCHAR(200) NOT NULL,
  status        VARCHAR(32)  NOT NULL DEFAULT 'active',
  retention_days INT         NOT NULL DEFAULT 365,
  created_at    DATETIME(6)  NOT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE users (
  id               BINARY(16)   NOT NULL,
  tenant_id        BINARY(16)   NOT NULL,
  external_subject VARCHAR(255) NOT NULL,
  status           VARCHAR(32)  NOT NULL DEFAULT 'active',
  ltm_enabled      TINYINT(1)   NOT NULL DEFAULT 1,  -- 用户级长期记忆开关
  created_at       DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_users_tenant_subject (tenant_id, external_subject),
  KEY ix_users_tenant (tenant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 3.2 会话与消息

```sql
CREATE TABLE conversations (
  id         BINARY(16)   NOT NULL,
  tenant_id  BINARY(16)   NOT NULL,
  owner_id   BINARY(16)   NOT NULL,
  title      VARCHAR(200) NULL,
  status     VARCHAR(32)  NOT NULL DEFAULT 'active',
  version    INT          NOT NULL DEFAULT 0,   -- 乐观锁/串行化版本
  created_at DATETIME(6)  NOT NULL,
  updated_at DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  KEY ix_conv_tenant_owner_updated (tenant_id, owner_id, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE messages (
  id              BINARY(16)  NOT NULL,
  tenant_id       BINARY(16)  NOT NULL,
  conversation_id BINARY(16)  NOT NULL,
  role            VARCHAR(32) NOT NULL,       -- user/assistant/system/tool
  content         MEDIUMTEXT  NOT NULL,
  token_count     INT         NOT NULL DEFAULT 0,
  sequence        INT         NOT NULL,
  created_at      DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_msg_conv_seq (conversation_id, sequence),
  KEY ix_msg_tenant_conv_seq (tenant_id, conversation_id, sequence),
  CONSTRAINT fk_msg_conv FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 3.3 Agent Run 与事件

```sql
CREATE TABLE agent_runs (
  id                            BINARY(16)   NOT NULL,
  tenant_id                     BINARY(16)   NOT NULL,
  conversation_id               BINARY(16)   NOT NULL,
  user_id                       BINARY(16)   NOT NULL,
  idempotency_key               VARCHAR(128) NOT NULL,
  request_fingerprint           CHAR(64)     NOT NULL,
  expected_conversation_version INT          NOT NULL,
  fencing_token                 BIGINT       NULL,
  status                        VARCHAR(32)  NOT NULL,
  user_message_id               BINARY(16)   NULL,
  assistant_message_id          BINARY(16)   NULL,
  route_json                    JSON         NULL,   -- 路由决策
  budgets_json                  JSON         NULL,   -- 预算快照
  versions_json                 JSON         NULL,   -- prompt/model/retrieval 版本
  error_code                    VARCHAR(100) NULL,
  created_at                    DATETIME(6)  NOT NULL,
  updated_at                    DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_run_tenant_idem (tenant_id, idempotency_key),
  KEY ix_run_conv_created (conversation_id, created_at),
  CONSTRAINT fk_run_conv FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE run_events (
  id         BINARY(16)  NOT NULL,
  tenant_id  BINARY(16)  NOT NULL,
  run_id     BINARY(16)  NOT NULL,
  sequence   INT         NOT NULL,
  event_type VARCHAR(64) NOT NULL,
  payload    JSON        NOT NULL,
  created_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_revent_run_seq (run_id, sequence),
  KEY ix_revent_tenant_run_seq (tenant_id, run_id, sequence),
  CONSTRAINT fk_revent_run FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 3.4 LangGraph Checkpoint（ADR-0004）

对齐 LangGraph checkpoint 协议，自研 MySQL Saver 使用两张表：

```sql
CREATE TABLE lg_checkpoints (
  tenant_id           BINARY(16)   NOT NULL,
  thread_id           VARCHAR(128) NOT NULL,   -- 通常为 run_id
  checkpoint_ns       VARCHAR(128) NOT NULL DEFAULT '',
  checkpoint_id       VARCHAR(64)  NOT NULL,
  parent_checkpoint_id VARCHAR(64) NULL,
  checkpoint          JSON         NOT NULL,   -- 序列化的 checkpoint
  metadata            JSON         NOT NULL,
  created_at          DATETIME(6)  NOT NULL,
  PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id),
  KEY ix_lgc_tenant_thread (tenant_id, thread_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE lg_checkpoint_writes (
  tenant_id     BINARY(16)   NOT NULL,
  thread_id     VARCHAR(128) NOT NULL,
  checkpoint_ns VARCHAR(128) NOT NULL DEFAULT '',
  checkpoint_id VARCHAR(64)  NOT NULL,
  task_id       VARCHAR(64)  NOT NULL,
  idx           INT          NOT NULL,
  channel       VARCHAR(128) NOT NULL,
  value         JSON         NOT NULL,
  created_at    DATETIME(6)  NOT NULL,
  PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 3.5 记忆

```sql
CREATE TABLE memory_summaries (
  id              BINARY(16)  NOT NULL,
  tenant_id       BINARY(16)  NOT NULL,
  conversation_id BINARY(16)  NOT NULL,
  level           VARCHAR(8)  NOT NULL,   -- L1/L2
  start_message_id BINARY(16) NOT NULL,
  end_message_id   BINARY(16) NOT NULL,
  source_version  INT         NOT NULL,
  summary         MEDIUMTEXT  NOT NULL,
  status          VARCHAR(32) NOT NULL DEFAULT 'active',
  created_at      DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  KEY ix_summary_conv_level (conversation_id, level, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE memory_items (
  id          BINARY(16)   NOT NULL,
  tenant_id   BINARY(16)   NOT NULL,
  user_id     BINARY(16)   NOT NULL,
  type        VARCHAR(32)  NOT NULL,   -- preference/entity_fact/goal/decision/successful_playbook
  content     TEXT         NOT NULL,
  source_refs JSON         NOT NULL,
  confidence  DECIMAL(4,3) NOT NULL,
  sensitivity VARCHAR(16)  NOT NULL DEFAULT 'normal',
  status      VARCHAR(32)  NOT NULL DEFAULT 'active',
  milvus_pk   VARCHAR(64)  NULL,       -- 对应 Milvus 记录主键
  expires_at  DATETIME(6)  NULL,
  created_at  DATETIME(6)  NOT NULL,
  updated_at  DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  KEY ix_mem_tenant_user_type (tenant_id, user_id, type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 3.6 文档与知识

```sql
CREATE TABLE knowledge_bases (
  id         BINARY(16)   NOT NULL,
  tenant_id  BINARY(16)   NOT NULL,
  name       VARCHAR(200) NOT NULL,
  status     VARCHAR(32)  NOT NULL DEFAULT 'active',
  created_at DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  KEY ix_kb_tenant (tenant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE documents (
  id                BINARY(16)  NOT NULL,
  tenant_id         BINARY(16)  NOT NULL,
  knowledge_base_id BINARY(16)  NOT NULL,
  logical_id        VARCHAR(128) NOT NULL,
  status            VARCHAR(32) NOT NULL DEFAULT 'active',
  created_at        DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  KEY ix_doc_tenant_kb (tenant_id, knowledge_base_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE document_versions (
  id             BINARY(16)   NOT NULL,
  tenant_id      BINARY(16)   NOT NULL,
  document_id    BINARY(16)   NOT NULL,
  version        INT          NOT NULL,
  source_ref     VARCHAR(512) NOT NULL,   -- 对象存储路径
  source_hash    CHAR(64)     NOT NULL,
  parser_version VARCHAR(32)  NOT NULL,
  chunker_version VARCHAR(32) NOT NULL,
  embedding_model VARCHAR(64) NOT NULL,
  language       VARCHAR(16)  NULL,
  effective_at   DATETIME(6)  NULL,
  expires_at     DATETIME(6)  NULL,
  security_level VARCHAR(16)  NOT NULL DEFAULT 'normal',
  acl_tokens     JSON         NOT NULL,
  status         VARCHAR(32)  NOT NULL DEFAULT 'processing', -- processing/indexed/published/rolled_back/deleted
  created_at     DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_docver (document_id, version),
  KEY ix_docver_tenant_status (tenant_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE citations (
  id                  BINARY(16)  NOT NULL,
  tenant_id           BINARY(16)  NOT NULL,
  answer_message_id   BINARY(16)  NOT NULL,
  document_version_id BINARY(16)  NOT NULL,
  chunk_id            VARCHAR(64) NOT NULL,   -- Milvus chunk 主键
  claim_id            VARCHAR(64) NOT NULL,
  created_at          DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  KEY ix_cit_answer (answer_message_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE knowledge_candidates (
  id            BINARY(16)  NOT NULL,
  tenant_id     BINARY(16)  NOT NULL,
  source_run_id BINARY(16)  NOT NULL,
  content       MEDIUMTEXT  NOT NULL,
  scores_json   JSON        NOT NULL,   -- Judge 各维度评分
  provenance    JSON        NOT NULL,   -- provenance_type + 原始证据
  review_status VARCHAR(32) NOT NULL DEFAULT 'pending', -- pending/approved/rejected/published/revoked
  created_at    DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  KEY ix_cand_tenant_status (tenant_id, review_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 3.7 Tool、反馈与审计

```sql
CREATE TABLE tool_invocations (
  id               BINARY(16)  NOT NULL,
  tenant_id        BINARY(16)  NOT NULL,
  run_id           BINARY(16)  NOT NULL,
  tool             VARCHAR(64) NOT NULL,
  args_digest      CHAR(64)    NOT NULL,
  auth_scope_digest CHAR(64)   NOT NULL,
  status           VARCHAR(32) NOT NULL,
  latency_ms       INT         NOT NULL,
  created_at       DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  KEY ix_tool_run (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE feedback (
  id         BINARY(16)  NOT NULL,
  tenant_id  BINARY(16)  NOT NULL,
  run_id     BINARY(16)  NULL,
  message_id BINARY(16)  NULL,
  rating     VARCHAR(16) NOT NULL,   -- up/down
  category   VARCHAR(64) NULL,
  comment    TEXT        NULL,
  created_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  KEY ix_fb_run (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE audit_logs (
  id         BINARY(16)   NOT NULL,
  tenant_id  BINARY(16)   NOT NULL,
  actor      VARCHAR(128) NOT NULL,
  action     VARCHAR(64)  NOT NULL,
  resource   VARCHAR(256) NOT NULL,
  result     VARCHAR(32)  NOT NULL,
  trace_id   VARCHAR(64)  NULL,
  detail     JSON         NULL,
  created_at DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  KEY ix_audit_tenant_created (tenant_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 3.8 Outbox / Inbox（ADR-0003）

```sql
CREATE TABLE outbox_events (
  id           BINARY(16)   NOT NULL,
  tenant_id    BINARY(16)   NOT NULL,
  aggregate    VARCHAR(64)  NOT NULL,   -- conversation/document/run...
  aggregate_id BINARY(16)   NOT NULL,
  topic        VARCHAR(128) NOT NULL,
  event_type   VARCHAR(64)  NOT NULL,
  payload      JSON         NOT NULL,
  published_at DATETIME(6)  NULL,       -- NULL = 未投递
  attempts     INT          NOT NULL DEFAULT 0,
  created_at   DATETIME(6)  NOT NULL,
  PRIMARY KEY (id),
  KEY ix_outbox_unpublished (published_at, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE consumer_inbox (
  consumer     VARCHAR(64) NOT NULL,
  event_id     BINARY(16)  NOT NULL,
  processed_at DATETIME(6) NOT NULL,
  PRIMARY KEY (consumer, event_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

所有业务表必须含 `tenant_id`。高频表（messages、run_events、audit_logs）按时间/tenant 分区做后续评估，不在 MVP 初期过早分区。

## 4. Milvus Collection（ADR-0002）

### 4.1 `knowledge_chunks`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `pk` | VARCHAR (主键) | `{document_version_id}:{child_id}` |
| `dense` | FLOAT_VECTOR(dim) | Embedding，HNSW 索引 |
| `sparse` | SPARSE_FLOAT_VECTOR | BM25 稀疏向量（内置 `BM25` function 生成），SPARSE_INVERTED_INDEX |
| `text` | VARCHAR | 子块文本（用于 BM25 analyzer + rerank） |
| `parent_id` | VARCHAR | 父块 id（父块扩展用） |
| `tenant_id` | VARCHAR | 标量过滤 |
| `knowledge_base_id` | VARCHAR | 标量过滤 |
| `document_version_id` | VARCHAR | 标量过滤 |
| `acl_tokens` | ARRAY<VARCHAR> | ACL 过滤（`ARRAY_CONTAINS`） |
| `status` | VARCHAR | published/shadow |
| `security_level` | VARCHAR | 密级过滤 |
| `effective_at` / `expires_at` | INT64 | 时效过滤（epoch ms） |
| `section_path` / `page` / `title` | VARCHAR/INT | 引用定位 |

- 索引：dense 用 HNSW（metric IP/COSINE）；sparse 用 SPARSE_INVERTED_INDEX（metric BM25）。
- Analyzer：中文分词（jieba/标准），在 collection schema 配置。
- Alias：`knowledge_active` / `knowledge_shadow`，发布时原子切换。
- 检索必带过滤表达式：`tenant_id == ? && status == "published" && ARRAY_CONTAINS_ANY(acl_tokens, ?) && effective_at <= now && (expires_at == 0 || expires_at > now)`。

### 4.2 `long_term_memory`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `pk` | VARCHAR | 对应 `memory_items.milvus_pk` |
| `dense` | FLOAT_VECTOR(dim) | 记忆内容向量 |
| `tenant_id` / `user_id` | VARCHAR | 标量过滤 |
| `type` | VARCHAR | 记忆类型 |
| `confidence` | FLOAT | 读取排序 |
| `status` | VARCHAR | active/revoked |

不存高敏原文；原文留在 MySQL `memory_items.content`。

## 5. Kafka Topic

| Topic | Key | 语义 |
| --- | --- | --- |
| `document.ingestion.requested` | document_version_id | 文档处理 |
| `document.indexed` | document_version_id | 索引完成 |
| `conversation.summary.requested` | conversation_id | 异步摘要 |
| `knowledge.distillation.requested` | run_id | 经验提炼 |
| `evaluation.requested` | evaluation_run_id | 离线评测 |
| `audit.event` | tenant_id | 审计汇聚 |
| `*.dlq` | 同源 key | 死信 |

at-least-once；消费者用 `consumer_inbox` + 业务唯一键幂等。

## 6. Redis Key 规范

| Key 模式 | 类型 | TTL | 用途 |
| --- | --- | --- | --- |
| `sa:lease:{tenant}:{conv}` | String | 租约 TTL | 会话租约（value=owner token） |
| `sa:fence:{tenant}:{conv}` | String(INCR) | 长期 | 单调递增 fencing token |
| `sa:run-events:{run}` | Stream | 保留窗口 | SSE 短期事件 |
| `sa:cache:tool:{tenant}:{tool}:{args_hash}:{scope_hash}` | String | 短 | Tool 结果缓存 |
| `sa:ratelimit:{tenant}:{user}` | String/ZSet | 窗口 | 限流计数 |

所有 Key 以 `sa:` + tenant 命名空间隔离。缓存值记录授权范围摘要，避免跨授权命中。

## 7. 数据删除与合规

按 user/tenant 删除时，异步清理：MySQL 业务表 → Milvus 记录（按 tenant_id/user_id 过滤删除）→ Redis 命名空间 Key → 对象存储原文。删除任务产生审计记录，24 小时内完成（NFR）。
