# 评审与验收清单

关联：[spec.md](./spec.md) · [tasks.md](./tasks.md)

## 1. 架构就绪

- [ ] API、Agent、Retrieval、Tool、Memory、Ingestion、Distillation 边界清晰。
- [ ] 领域层不依赖 FastAPI、SQLAlchemy、LangGraph、Milvus SDK 和模型 SDK。
- [ ] 任意实例可恢复会话，不存在进程内关键状态。
- [ ] 幂等键、Lease、fencing token、CAS、Outbox 组合通过并发评审。
- [ ] MySQL、Redis、Milvus、Kafka、对象存储有容量和 HA 方案。
- [ ] Milvus 索引版本、alias 切换、回滚、全量重建流程已验证。
- [ ] 模型、Embedding、Rerank、Tool 均有 Adapter、超时、熔断、降级。
- [ ] MySQL 迁移满足滚动升级兼容性（expand/migrate/contract）。
- [ ] LangGraph MySQL Checkpointer 通过契约测试与并发恢复测试。

## 2. RAG 质量

- [ ] 文档标题、表格、列表、页码、版本、ACL 正确保留。
- [ ] Parent-Child 参数经样本文档与 Golden Set 调优。
- [ ] Milvus dense 与 sparse BM25 双路真正并行，且召回前标量 ACL 过滤。
- [ ] RRF、去重、父块扩展、Cross-Encoder、Evidence Packing 可观测。
- [ ] HyDE 仅按条件触发，假设内容不进入引用。
- [ ] 反思最多执行配置次数，无无界循环。
- [ ] Unsupported Claim 与 Citation Entailment 校验生效。
- [ ] 证据不足/冲突/过期时能澄清、展示冲突或拒答。
- [ ] Recall@20、NDCG@10、Citation Precision、Groundedness 达门槛。

## 3. Multi-Agent 与记忆

- [ ] 快速路径不会无条件调用 Planner 或所有 Worker。
- [ ] ExecutionPlan 通过 Schema、DAG、权限、预算、工具白名单校验。
- [ ] 六类 Worker 独立 Prompt、上下文视图、输出 Schema。
- [ ] Intent Analyst 不输出临床判断、受保护属性或无依据心理结论。
- [ ] Supervisor 中立合成，Worker 人格不跨任务传播。
- [ ] 最近消息、滚动摘要、长期记忆按 Token Budget 装配。
- [ ] 异步摘要带来源区间和版本，不覆盖更新状态。
- [ ] 长期记忆经稳定性/敏感性/去重/冲突/过期门禁。
- [ ] 用户可禁用长期记忆，租户可配置保留和删除。
- [ ] 长对话、跨实例、摘要失真、记忆污染测试通过。

## 4. Tool 与业务数据

- [ ] 模型无法伪造 tenant/user/auth scope 或服务端 Context。
- [ ] 每个 Tool 有输入输出 Schema、权限、超时、重试、敏感级别。
- [ ] MVP Tool 仅允许只读或纯计算。
- [ ] Tool 返回字段裁剪、脱敏、大小限制、时效标注。
- [ ] Tool 缓存键含 tenant、参数、授权范围、数据版本。
- [ ] 外部 API 契约测试覆盖正常/空数据/超时/限流/错误。
- [ ] 实时数据不可用时明确降级，不生成模拟结果。
- [ ] 所有 Tool 调用有 Trace 和审计记录。

## 5. 知识回流

- [ ] 候选可追溯到原始对话、证据、引用、反馈。
- [ ] PII 和禁止回流数据在 Judge 前清理。
- [ ] Judge 输出结构化评分并经人工样本校准。
- [ ] Checker 覆盖重复/矛盾/时效/权限/恶意内容。
- [ ] 模型不能直接发布候选知识。
- [ ] 发布前 Shadow 评测，发布用版本和原子切换。
- [ ] 撤销后检索、缓存、后续回流不再使用该知识。
- [ ] 模型生成知识无无权威来源的自循环引用。
- [ ] 高价值知识回流误发布数 = 0。

## 6. 安全与隐私

- [ ] OIDC/OAuth2、RBAC+ABAC、服务间身份接入。
- [ ] MySQL、Milvus、Redis、对象存储均验证租户隔离。
- [ ] Prompt Injection、越权 Tool、任意 URL/SQL、数据外泄测试通过。
- [ ] Secret 由 Secret Manager 管理，不入代码/镜像/日志。
- [ ] 传输和存储加密启用。
- [ ] 日志、Trace、Metric、评测样本脱敏。
- [ ] 审计日志覆盖认证/数据访问/Tool/模型/管理/发布。
- [ ] 用户/租户数据删除可在 24 小时内完成并出结果。
- [ ] 限流、配额、最大输入、Token、并发限制可防资源耗尽。
- [ ] 依赖、镜像、代码安全扫描无未豁免高危问题。

## 7. 可靠性与运维

- [ ] API 可用性、延迟、错误率、质量指标均有 Dashboard。
- [ ] 告警采用 SLO Burn Rate 并关联 Runbook。
- [ ] 模型/Embedding/Rerank/Milvus/Tool 故障降级已演练。
- [ ] Redis Lease 丢失和实例强杀不产生重复最终消息。
- [ ] Kafka 重复投递不重复发布知识或重复处理文档。
- [ ] MySQL 恢复、Milvus 重建、对象存储恢复已演练。
- [ ] HPA、PDB、Probe、优雅退出、节点驱逐场景通过。
- [ ] Canary 可按版本停止、回滚。
- [ ] 数据库和索引变更具备向前/向后兼容策略。
- [ ] 容量和长稳测试达标，无持续资源泄漏。

## 8. 测试门禁

- [ ] Ruff 和类型检查通过。
- [ ] 核心领域单元测试覆盖率 >= 85%，全仓行覆盖率 >= 75%。
- [ ] 真实依赖容器集成测试通过。
- [ ] 模型和业务 API 契约测试通过。
- [ ] 六类 Worker 端到端场景全部通过。
- [ ] 权限过滤正确率 100%。
- [ ] >=500 条 Golden Set 自动评测通过。
- [ ] >=10% Golden Set 完成业务专家盲审。
- [ ] 负载、故障注入、安全、回滚测试通过。
- [ ] 无未关闭的 P0/P1 缺陷。

## 9. 本地开发就绪

- [ ] `make up` 一键起全栈成功。
- [ ] `make migrate` / `make seed` 成功。
- [ ] `make test` 单元测试不依赖外部服务。
- [ ] `make test-int` 集成测试基于 compose 栈通过。
- [ ] 直连外部模型网关的配置文档清晰。
