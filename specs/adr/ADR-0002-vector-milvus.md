# ADR-0002 向量检索选用 Milvus，BM25 用 Milvus 原生稀疏检索

| 属性 | 内容 |
| --- | --- |
| 状态 | 已接受 |
| 决策日期 | 2026-08-11 |
| 影响范围 | RAG 双路召回、文档索引、长期记忆检索 |

## 背景

Agentic RAG 需要"向量语义召回 + BM25 关键词召回"双路混合，再经 RRF 粗排和 Cross-Encoder 重排。需要一个能承载大规模向量 KNN、支持标量过滤（tenant/ACL/status/时效）的检索引擎，并尽量收敛组件数量。

## 可选项

1. **Milvus（向量）+ Elasticsearch/OpenSearch（BM25）**：两个成熟引擎，但需维护双写与一致性，组件多。
2. **Milvus 2.5+ 单引擎**：向量 KNN（HNSW/IVF）+ 内置稀疏向量全文检索（BM25 函数，2.5 GA），双路在同一引擎内完成。
3. **纯向量（只做语义召回）**：省组件，但丢失关键词精确匹配能力，命中率下降。

## 决策

选用 **Milvus 2.5+ 作为唯一检索引擎**：
- 密集向量（dense）承载语义召回，使用 HNSW 索引。
- 稀疏向量（sparse）承载 BM25 关键词召回，使用 Milvus 内置 `BM25` 函数 + `SPARSE_INVERTED_INDEX`。
- 两路召回结果在应用层做加权 RRF 融合、父块扩展、去重，再送 Cross-Encoder 重排。
- 标量字段（tenant_id、acl_tokens、status、effective_at、security_level 等）作为 Milvus 标量过滤条件，**在召回前过滤**，杜绝先召回后过滤的越权风险。

## 关键约束

1. Collection 按 `knowledge_chunks` 与 `long_term_memory` 分离；版本切换用 alias（`knowledge_active` / `knowledge_shadow`）。
2. 中文 BM25 依赖 Milvus analyzer（jieba/标准分词），需在 collection schema 配置 analyzer。
3. Embedding 维度与模型版本写入 chunk 元数据；换模型走新版本 collection，不原地改。
4. 检索查询必须携带 `tenant_id` 与 ACL 过滤表达式，无租户上下文的检索接口一律禁止。
5. Retriever 通过领域 Port 抽象，Milvus 为其一个实现，保留未来替换空间。

## 后果

- 正面：单一检索引擎完成双路召回，组件数收敛；标量过滤天然前置保证隔离；社区活跃、云托管成熟。
- 负面：Milvus BM25 分词/相关性调优能力弱于专业 ES；本地部署需 etcd + MinIO 依赖（standalone 模式已打包）。
- 缓解：RRF 权重、BM25 参数、rerank 均走动态配置可调（ADR-0005）；离线 Golden Set 评测驱动调参。
