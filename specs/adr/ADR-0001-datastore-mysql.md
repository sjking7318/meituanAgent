# ADR-0001 事务存储选用 MySQL 8

| 属性 | 内容 |
| --- | --- |
| 状态 | 已接受 |
| 决策日期 | 2026-08-11 |
| 影响范围 | 全部事务数据、并发控制、迁移、部署 |

## 背景

系统需要一个强一致的事务数据库承载会话、消息、Agent Run、记忆元数据、文档元数据、知识回流候选、审计日志、Outbox 事件和动态配置。核心诉求是多实例无状态服务下的并发控制：幂等去重、乐观锁 CAS、行级悲观锁、唯一约束。团队与运维现有基础设施以 MySQL 为主。

## 可选项

1. **PostgreSQL**：功能强（原生 UUID、JSONB、`SELECT FOR UPDATE SKIP LOCKED`、丰富扩展），但与现有运维栈不符。
2. **MySQL 8.x (InnoDB)**：团队标准，支持事务、行锁、唯一约束、乐观锁；生态成熟、云托管完善。
3. **NewSQL（TiDB 等）**：水平扩展强，但本地开发重、运维复杂度高，MVP 不必要。

## 决策

选用 **MySQL 8.x（InnoDB 引擎）** 作为唯一事务存储，同时承载 LangGraph Checkpoint（见 ADR-0004）、Outbox（见 ADR-0003）和动态配置（见 ADR-0005）。ORM 使用 SQLAlchemy 2 Async + `asyncmy` 驱动，迁移使用 Alembic。

## 关键约束与规范

为规避 MySQL 与 PostgreSQL 的差异，统一以下工程规范：

1. **主键/外键 UUID**：统一以 `BINARY(16)` 存储（应用层生成 UUIDv7 保证时间有序、减少索引页分裂）。禁止用 `CHAR(36)`。
2. **时间**：统一 `DATETIME(6)`，应用层写入 UTC，不使用数据库时区转换；不用 `TIMESTAMP`（2038 问题 + 隐式时区）。
3. **字符集**：库/表/连接统一 `utf8mb4` + `utf8mb4_0900_ai_ci`。
4. **乐观锁 CAS**：以 `UPDATE ... SET version=version+1 WHERE id=? AND version=?` 的 `rowcount` 判定冲突。
5. **悲观锁**：会话串行化用 `SELECT ... FOR UPDATE`。
6. **幂等**：`(tenant_id, idempotency_key)` 唯一约束，插入冲突捕获 `IntegrityError` 转幂等复用。
7. **队列取任务**：需要时使用 `FOR UPDATE SKIP LOCKED`（MySQL 8.0+ 支持）。
8. **JSON**：使用原生 `JSON` 类型存放结构化 payload（RunState 快照、事件 payload、评分等）。
9. **迁移**：遵循 expand/migrate/contract，兼容滚动升级；DDL 变更评估 Online DDL / gh-ost。

## 后果

- 正面：贴合团队运维；事务与并发原语齐备；本地/云一致。
- 负面：无 `JSONB` 索引能力、无原生数组类型，复杂查询需在应用层处理；需自研 MySQL LangGraph checkpointer（官方不提供）。
- 缓解：所有数据库访问经 Repository/Port 隔离，领域层不感知 MySQL 细节，未来可替换。
