# ADR-0003 异步事件总线选用 Kafka + MySQL Outbox

| 属性 | 内容 |
| --- | --- |
| 状态 | 已接受 |
| 决策日期 | 2026-08-11 |
| 影响范围 | 文档处理、异步摘要、知识回流、评测、审计汇聚 |

## 背景

多个能力需要异步解耦：文档解析/切分/索引、滚动摘要生成、知识经验提炼与回流、离线评测、审计汇聚。要求：不阻塞在线主链路、可水平扩容消费者、至少一次投递且业务幂等、可回溯与重放。

## 可选项

1. **Redis Streams**：轻量、复用现有 Redis，但持久化与大吞吐、多消费组治理弱于专业 MQ。
2. **Kafka**：行业标准，分区扩容、消费组、重放、生态成熟；本地可用 Redpanda 兼容 Kafka 协议轻量替代。
3. **数据库轮询队列**：实现简单，但吞吐与实时性差，MySQL 压力大。

## 决策

选用 **Kafka** 作为异步事件总线，配合 **MySQL Outbox 模式**保证"业务写入与事件发布"的原子性：

1. 在线事务中，业务数据与 `outbox_events` 记录同一 MySQL 事务提交。
2. 独立的 Outbox Relay 进程轮询/CDC 未发布事件，投递到 Kafka 后标记已发布。
3. 消费者用 `consumer_inbox`（consumer + event_id 唯一）+ 业务唯一键实现幂等，交付语义为 at-least-once，不宣称 exactly-once。
4. 本地开发使用 **Redpanda**（Kafka 协议兼容，单容器、低内存），生产使用托管 Kafka。

## Topic 规划（初版）

| Topic | Key | 语义 |
| --- | --- | --- |
| `document.ingestion.requested` | document_version_id | 文档处理请求 |
| `document.indexed` | document_version_id | 索引完成 |
| `conversation.summary.requested` | conversation_id | 异步摘要 |
| `knowledge.distillation.requested` | run_id | 经验提炼 |
| `evaluation.requested` | evaluation_run_id | 离线评测 |
| `audit.event` | tenant_id | 审计汇聚 |

## 后果

- 正面：主链路不丢最终消息（Outbox 兜底）；消费者独立扩容；可重放与死信治理。
- 负面：引入 Kafka 运维成本；Outbox Relay 需保证单一投递者或幂等投递。
- 缓解：Relay 用行锁/租约保证有序单投递；所有消费者强制幂等；EventBus 抽象为 Port，未来可切换实现。
