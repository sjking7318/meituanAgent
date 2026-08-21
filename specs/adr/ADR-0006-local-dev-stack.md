# ADR-0006 本地开发全栈与外部模型直连

| 属性 | 内容 |
| --- | --- |
| 状态 | 已接受 |
| 决策日期 | 2026-08-11 |
| 影响范围 | 本地开发、测试、CI |

## 背景

团队需要在本机进行部署、开发和测试。系统依赖 MySQL、Redis、Milvus、Kafka、对象存储等多个中间件。需要一键起全栈、快速迭代，同时测试不应强依赖外部服务。模型侧已决定：本地开发直连公司/外部 OpenAI 兼容 API。

## 决策

### 本地依赖：docker-compose 一键全栈

提供 `docker-compose.yml` 覆盖：

| 组件 | 本地镜像 | 说明 |
| --- | --- | --- |
| MySQL 8 | `mysql:8.4` | 事务/Checkpoint/Outbox/配置 |
| Redis | `redis:7`（或 8） | Lease/SSE/缓存/限流 |
| Milvus standalone | `milvusdb/milvus` + etcd + MinIO | 向量 + 原生 BM25 |
| Kafka | `redpanda`（Kafka 协议兼容） | 异步事件总线 |
| 对象存储 | MinIO（Milvus 已含，可复用） | 文档原文 |

`make up` 起全栈，`make migrate` 跑迁移，`make seed` 灌 tenant/user/动态配置种子，`make dev` 起应用。

### 模型接入：直连外部 API

- 本地默认 `MODEL_PROVIDER=openai_compatible`，指向公司/外部网关。
- 连接参数（base_url、api_key、model_name）走一级静态配置（`.env`），密钥不入库、不入镜像、不入日志。
- 模型路由（哪个任务用哪个模型）走二级动态配置。
- 保留 Mock Provider 供单元测试与离线 CI 使用（不依赖网络）。

### 测试分层与依赖

- **单元测试**：不依赖任何外部服务，Mock 所有 Port（含模型）。
- **集成测试**：依赖 docker-compose 起的真实 MySQL/Redis/Milvus/Kafka。
- **契约测试**：校验模型网关、业务 API、Tool 的 Schema 与错误语义。

## 后果

- 正面：一键起全栈，开发闭环快；模型直连效果真实；单测无网络依赖稳定。
- 负面：本机资源占用较高（Milvus + Kafka 较重）；直连外部模型有成本与网络依赖。
- 缓解：提供精简 profile（可仅起 MySQL+Redis 跑核心并发测试）；模型调用有预算与超时限制；CI 用 Mock。
