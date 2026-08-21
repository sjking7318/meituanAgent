# 美团销售智能助手 · 自顶向下完整讲解

> 本文跟着**一条真实请求**从 HTTP 入口一路走到数据库落库，逐层讲透每个环节的
> 输入输出、数据结构变换、控制流与设计原因，并指向真实代码位置（file:line）。
> 读法：从头顺读即可建立完整链路认知。约定用 `[文件:行]` 指代码，点开即达。

---

## 0. 先建立坐标系：一条请求会穿过哪些层

用户在网页里发一句"新商家首月的佣金政策是什么？"，这句话会依次穿过 6 层：

```
① Web UI (web/index.html)                     浏览器发 POST + SSE
      │  HTTP
② API 层 (api/routes.py)                       鉴权、请求校验、依赖注入取 service
      │  调用
③ 应用编排 (application/conversation_service)  租约→幂等→事务→跑Agent→落库→发事件
      │  调用 AgentExecutor.run()
④ Agent 运行时 (agents/runtime/graph.py)        LangGraph 图：supervisor→worker→synthesize
      │  节点内调用
⑤ 应用服务 (retrieval / memory) + 端口          检索链路、记忆装配
      │  依赖 domain/ports 的 Protocol
⑥ 基础设施适配器 (infrastructure/*)             Milvus、MySQL、Redis、DashScope、Skills
```

每一层只依赖它下面一层的**抽象**（domain 的 Protocol），不依赖具体实现——这就是
六边形架构。下面逐层展开。

---

## 1. 入口：API 层做了什么

**代码**：[api/routes.py](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/api/routes.py)

### 1.1 请求进来先过鉴权
所有 `/v1/*` 端点依赖 `Auth = Depends(get_auth_context)`。`get_auth_context`
[api/routes.py:180](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/api/routes.py#L180) 从 header 取 `X-Tenant-ID` / `X-User-ID`，
交给 `Authenticator.authenticate` [api/auth.py:20](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/api/auth.py#L20)。
- **disabled 模式**（本地）：信任 header，解析成 `AuthContext(tenant_id, user_id)`。
- **oidc 模式**（生产）：目前是占位，直接拒绝——生产必须实现 JWT 校验。
- **设计原因**：鉴权做成端口，域层只拿到一个已验证的 `AuthContext`，不关心怎么验的。

### 1.2 发消息端点
`POST /v1/conversations/{id}/messages` → `send_message`
[api/routes.py:286](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/api/routes.py#L286)。关键点：
- **`Idempotency-Key` header 必填**（`min_length=8`），这是幂等的钥匙。
- 请求体 `SendMessageRequest`（`content` + `stream`），Pydantic 校验非空、去空白
  [api/routes.py:59](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/api/routes.py#L59)。
- `stream=true` → 返回 `StreamingResponse`（SSE，边跑边推事件）；否则跑完一次性返回。
- 端点本身**只做转发**：从 `request.app.state.container` 取 `ConversationService`，把参数递进去。
  业务逻辑一点不写在路由里——路由是"薄"的。

### 1.3 依赖注入容器
所有 service 在启动时由 `build_container()`
[main.py:52 起](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/main.py) 组装好，挂在
`app.state.container`。这里就是"把抽象接口和具体实现接起来"的唯一地方：
Milvus 实现塞给 RetrievalService、DashScope 网关塞给 AgentRuntime、SkillLibrary
塞给图……换实现只改这一处。

---

## 2. 编排核心：ConversationService.send_message 的 11 步

**代码**：[application/conversation_service.py:137](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/application/conversation_service.py#L137)

这是整个系统最关键的方法。它要在**多实例并发**下保证：同一会话不被两个实例同时
处理、重复请求不重复执行、跑到一半实例挂了不会写脏数据。逐步看：

### 步骤 1：算幂等指纹
`_fingerprint(conversation_id, content)` [conversation_service.py:518](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/application/conversation_service.py#L518)
= `sha256(conversation_id + content)`。同一 Idempotency-Key 必须配同样的指纹，
否则说明 key 被复用到不同请求上 → 报 `IdempotencyConflictError`。

### 步骤 2：拿 Redis 租约（进临界区）
`async with self._lease_manager.hold(tenant_id, conversation_id) as lease`
[conversation_service.py:147](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/application/conversation_service.py#L147)。
- 租约保证**同一会话同一时刻只有一个实例在处理**。
- 租约带 **fencing token**（单调递增的数字，`lease.fencing_token`）——这是防"僵尸实例"的关键，后面步骤 8 用到。
- 实现：Redis SET NX + 续租，见 `infrastructure/redis/lease.py`。

### 步骤 3：幂等检查
`_prepare_run` [conversation_service.py:312](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/application/conversation_service.py#L312)
先查 `runs.get_by_idempotency_key`。命中过就直接 `_build_existing_result`
把上次的结果原样返回（`replayed=True`）——**不重复跑模型**。这是幂等的正路径。

### 步骤 4：开事务，乐观锁推进版本 + 建 run
还在 `_prepare_run` 里：
- `conversations.bump_version(old_version)` [conversation_service.py:344](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/application/conversation_service.py#L344)：
  `UPDATE ... WHERE version = old_version`，`rowcount != 1` 就抛 `ConcurrentWriteError`。
  这是 **MySQL 侧的乐观锁 CAS**。
- 写入 user `Message`，建 `AgentRun`（状态置 `RUNNING`，记下 `fencing_token` 和
  `expected_conversation_version`）。
- `message.sequence` 用 `version*2-1`（user）/`version*2`（assistant）编号，保证严格递增可分页。

### 步骤 5：发 run.started 事件（SSE）
`_emit(run.id, "run.started", ...)` [conversation_service.py:173](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/application/conversation_service.py#L173)。
`_emit` [:479](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/application/conversation_service.py#L479) 把事件
`append` 进 Redis Stream，并（若是流式请求）回调推给前端。落库失败也不炸主流程（catch 掉）。

### 步骤 6：装配历史（记忆）
`_load_history` [conversation_service.py:446](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/application/conversation_service.py#L446)：
如果注入了 `MemoryService`，调它的 `load_context`（见 §6）拿到「摘要 + 最近窗口」；
否则回退到"取最近 16 条"。返回 `tuple[ModelTurn, ...]`。

### 步骤 7：跑 Agent
`self._agent_executor.run(run_id, tenant_id, user_id, conversation_id, user_query, history)`
[conversation_service.py:189](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/application/conversation_service.py#L189)。
这里 `_agent_executor` 是个 **Protocol**（`AgentExecutor` [:44](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/application/conversation_service.py#L44)），
真实实现是 `AgentRuntime`——**编排层不知道底下是 LangGraph**，只知道"给我个 `AgentOutcome`"。
跑失败 → 标记 `run.failed`，抛 `DependencyUnavailableError`（对应你之前看到的 `DEPENDENCY_UNAVAILABLE`）。
进入 §4/§5。

### 步骤 8：双重校验（防旧实例覆盖）
Agent 跑完后、写结果前，两道关卡：
1. `lease.ensure_valid()` [conversation_service.py:212](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/application/conversation_service.py#L212)：
   租约还在我手里吗？丢了就 `run.conflicted` + `LEASE_LOST`。
2. 事务内 `lock_at_version(expected+1)` [:234](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/application/conversation_service.py#L234)：
   会话版本没被别人抢吗？被抢就 `STALE_FENCING_VERSION`。
- **为什么要两道**：Agent 可能跑很久，期间租约可能过期、别的实例可能已经推进了会话。
  没有这两道，一个"复活的旧实例"会用过时结果覆盖新数据。

### 步骤 9：事务内写 assistant 消息 + run→succeeded
[conversation_service.py:232-250](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/application/conversation_service.py#L232)：
assistant `Message`（`content` + `citations` + `token_count`）落库，`run` 状态转
`SUCCEEDED`，`assistant_message_id` 回填，一起 commit。**一致性**：消息和 run 状态同一事务。

### 步骤 10：发 message.completed 事件

### 步骤 11：推进滚动摘要
`self._memory_service.maybe_summarize(tenant_id, conversation_id)`
[conversation_service.py:276](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/application/conversation_service.py#L276)。
超阈值就生成新摘要（见 §6）。失败静默，不影响已经成功的这轮回答。

> **小结**：这一层的全部复杂度都是为了"多实例安全"。幂等唯一约束防重复、Redis 租约
> 防并发、fencing token + CAS 防旧实例覆盖、事务保一致、Redis Stream 保断线可恢复。

---

## 3. Agent 运行时入口：AgentRuntime.run

**代码**：[agents/runtime/graph.py:358](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L358)

编排层调 `run()` 后，这里做三件事：

1. **建初始状态** `initial_state(...)` → `RunState`（TypedDict，[state.py:12](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/state.py#L12)）。
   `RunState` 只装结构化字段（query/route/evidence/answer/citations…），
   **prompt 全文、大结果绝不进 state**——因为 state 要落 checkpoint，越小越好。
2. **构造 `RunnableConfig`** [graph.py:375](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L375)：
   - `configurable.thread_id = str(run_id)`：checkpoint 按 run_id 存取，任意实例可恢复。
   - `configurable.history`：把历史序列化成 `(role, content)` 塞进去（**瞬时上下文，不进 checkpoint**）。
   - `callbacks`：若开了追踪，注入 Langfuse handler（见 §8）。
3. **`await self._graph.ainvoke(state, config)`** [graph.py:394](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L394)：
   跑图，结果映射成 `AgentOutcome`（answer/model/tokens/citations）返回给编排层。

**图的结构**（`_build` [graph.py:136](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L136)）：

```
START → supervisor ─(条件边 _route)─┬→ retrieve → knowledge_qa ─┐
                                    ├→ chitchat ────────────────┤
                                    ├→ skill ───────────────────┤→ synthesize → END
                                    └→ clarify ──────────────────┘
```

图在 `__init__` 时 `.compile(checkpointer=...)` [graph.py:134](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L134)，
checkpointer 是自研的 MySQL 版（见 §7）。

---

## 4. 第一个节点：supervisor 怎么决定走哪条路

**代码**：`_supervise` [graph.py:170](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L170) → `_classify` [graph.py:183](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L183)

这是"意图分析师"节点。它要在 **skill / knowledge_qa / chitchat / clarify** 里选一条。
`_classify` 的逻辑分四段：

1. **确定性护栏**：`len(query.strip()) <= 2` 直接判 `clarify`
   [graph.py:194](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L194)。
   原因：真机 LLM 对"？"这种超短输入分类不稳，用规则兜住。
2. **注入 level-1 skill catalog**：把每个 skill 的 `name: description` 拼进 prompt
   [graph.py:197](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L197)。
   **这是渐进式 Skills 的关键**——只注入几十 token 的清单，不加载正文（见 §9）。
3. **LLM 分类**：调 `_SUPERVISOR_SYSTEM_PROMPT`，模型输出 `skill:<名>` 或基础意图标签
   [graph.py:200-217](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L200)。
   命中且 skill 名在 catalog 里 → 返回 `{"skill": name}`。
4. **启发式兜底**：LLM 挂了或输出没法用 → `_heuristic_skill`（关键词命中 skill）
   [graph.py:60](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L60)
   → `_heuristic_intent`（寒暄词/短句/其余）[graph.py:67](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L67)。
   兜底保证 **Mock 模型下测试确定性**。

**输出**：写进 `RunState.route`（`{"skill": ...}` 或 `{"primary_worker": ...}`）。

**路由函数** `_route` [graph.py:176](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L176)：
读 `route`，有 `skill` 走 skill 节点，否则按 `primary_worker` 走。非法值兜底到 knowledge_qa。

**人格隔离（防漂移核心）**：意图分析师这个"人格"只活在 supervisor 节点的 prompt 里，
`route` 里只存一个纯标签，**绝不把人格指令带进下游 worker**。每个 worker 的 prompt
都从固定模板重新拼（`_KNOWLEDGE_QA_SYSTEM_PROMPT` 等是模块级常量），不继承上文人格。

---

## 5. 主路径：retrieve → knowledge_qa（Agentic RAG 全链路）

假设 supervisor 判成 `knowledge_qa`，先进 `retrieve` 节点。

### 5.1 retrieve 节点：调检索服务
`_retrieve` [graph.py:294](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L294)
调 `RetrievalService.retrieve()`，把返回的 `Evidence` 列表拍平成 dict 写进
`RunState.evidence`（**只存结构化字段进 state**）。

### 5.2 检索链路内部（RetrievalService.retrieve）
**代码**：[application/retrieval_service.py:85](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/application/retrieval_service.py#L85)

数据一步步变换：

```
query(str)
  │ embed（DashScope 原生 embedding）
query_vector(list[float])
  │ 双路并行召回（各 Top 50），两路都带 ACL 标量前置过滤
dense: list[Candidate]   bm25: list[Candidate]
  │ 加权 RRF 融合（按 chunk_id）
fused: list[(Candidate, score)]  → 取 Top 40
  │ Cross-Encoder rerank（DashScope）
候选重排 → Top 8
  │ _to_evidence
evidence: list[Evidence]
```

关键点：
- **双路召回** [retrieval_service.py:97,104](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/application/retrieval_service.py#L97)：
  `dense_recall`（向量语义）+ `bm25_recall`（关键词精确）。两路互补。
- **加权 RRF** `reciprocal_rank_fusion` [retrieval_service.py:43](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/application/retrieval_service.py#L43)：
  `score(d) = Σ_i weight_i / (k + rank_i(d))`，`k=60`。按 `chunk_id` 聚合两路排名。
  为什么用 RRF：不需要两路分数可比，只用排名，鲁棒。
- **rerank 降级** [retrieval_service.py:118-130](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/application/retrieval_service.py#L118)：
  reranker 挂了不报错，`reranked=False` 降级用 RRF 顺序。**可用性优先**。
- **ACL 前置过滤**：在 Milvus 侧（见 §10.2）召回**前**就按 tenant/status/时效/acl_tokens
  过滤，杜绝越权数据被召回。

### 5.3 knowledge_qa 节点：证据门禁 + 带引用生成
**代码**：`_knowledge_qa` [graph.py:313](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L313)

1. 从 state 把 evidence dict 还原成 `Evidence` 对象。
2. **证据门禁** [graph.py:327](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L327)：
   `if not evidence: return _ABSTAIN_ANSWER`。**没证据就拒答，绝不臆造**——防幻觉的硬闸门。
3. 有证据：`_format_evidence` [graph.py:89](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L89)
   把证据编号成 `[1] 标题/章节\n正文`，拼进 prompt；system prompt 要求"每条结论用
   [编号] 标注来源"。
4. 调模型生成，`_citations_from` [graph.py:101](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L101)
   产出结构化 citations（marker/chunk_id/document_version_id/title/section_path/page），
   写进 `RunState.citations`。这就是前端能显示"[1] 佣金政策 · 首月免佣"的来源。

### 5.4 chitchat / clarify / skill 三条旁路
- **chitchat** [graph.py:225](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L225)：
  history-aware（带 `_history_from_config` 的历史），能回答"你还记得我说过…"。无证据、无引用。
- **clarify** [graph.py:245](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L245)：
  直接返回固定澄清语，**不调模型**（省成本，快）。
- **skill** [graph.py:253](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L253)：见 §9。

### 5.5 synthesize 节点
`_synthesize` [graph.py:353](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L353)：
当前是单 worker 路径，直接把 worker 的 answer 作为最终答案。多 worker 中立合成是后续里程碑。

---

## 6. 记忆是怎么装配和推进的

**代码**：[application/memory_service.py](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/application/memory_service.py)

两个方向，对应步骤 6（读）和步骤 11（写）：

### 读：load_context（装配上文）
- 取最近 `recent_turns*2` 条原始消息（滑动窗口）+ 最新一条滚动摘要。
- **只注入摘要中未被窗口覆盖的部分**（`summary.covered_through_sequence < 最老窗口消息的 sequence`）
  才把摘要作为一条 `MessageRole.SYSTEM` 的 turn 放最前面，避免和原始消息重复。
- 返回 `(摘要 turn?) + 最近 user/assistant turns`。

### 写：maybe_summarize（推进摘要）
- 取全部消息，算出"超出窗口、还没被摘要覆盖"的那批。
- 数量超过阈值（`STM_SUMMARY_TRIGGER_TURNS`）才触发：调 LLM 把这批 + 旧摘要
  **增量合并**成新摘要文本。
- 写 `conversation_summaries` 新版本：`source_version = 旧版本+1`。写前再查一次，
  **若已有更新的版本就放弃**（并发下不覆盖别人写的更新摘要）。
- 失败静默（`try/except` + warning）——记忆维护绝不能拖垮主回答。

**为什么这么设计**：状态全在 MySQL，不在进程内存 → **任意实例装配出的上文一致**；
版本化不覆盖 → 防并发写坏、可回溯。

---

## 7. LangGraph 状态怎么落 MySQL（checkpoint）

**代码**：`MySQLCheckpointSaver` [agents/runtime/checkpointer.py](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/checkpointer.py)

- 实现 LangGraph `BaseCheckpointSaver` 的 async 版：`aput` / `aput_writes` /
  `aget_tuple` / `alist` / `adelete_thread`。
- `thread_id = str(run_id)`，`checkpoint_ns` 支持子图。
- checkpoint / metadata 用 LangGraph 的序列化协议存进 MySQL `lg_checkpoints` /
  `lg_checkpoint_writes`（blob 列）。
- **意义**：图执行的每一步状态都持久化。实例 A 跑到一半挂了，实例 B 能用同一个
  `run_id` 从最近 checkpoint 恢复继续跑，配合 fencing token 防旧实例回写。

---

## 8. 渐进式 Skills：三级按需加载（Claude-Code 风格）

**代码**：库 [infrastructure/skills/library.py](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/infrastructure/skills/library.py)；
skill 内容在仓库根 `skills/<name>/SKILL.md`。

核心思想：**上下文平时只有一份轻量清单，用到才逐级展开**，让 skill 库无限大也不涨常驻预算。

| Level | 方法 | 内容 | 何时读 | 出现在链路哪一步 |
| --- | --- | --- | --- | --- |
| 1 | `catalog()` | 每个 skill 的 name+description | 启动即注入 supervisor | §4 `_classify` 拼 catalog_text |
| 2 | `load(name)` | 该 skill 的 `SKILL.md` 正文 | 命中 skill 时才读 | §8 `_skill` 节点 |
| 3 | `read_resource()` | 正文引用的模板/脚本 | 模型真用到才读 | 由模型按需触发，含路径穿越防护 |

**skill 节点** `_skill` [graph.py:253](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L253)：
- `self._skill_library.load(skill_name)` 读正文（level-2）。
- 把正文作为 **system prompt**（"你正在执行技能「X」，严格遵循以下操作说明：…"），
  附上可按需引用的资源文件名（level-3 提示，不主动读内容）。
- 带历史调模型，产出答案。

**FileSystemSkillLibrary**：
- `catalog()` [library.py:52](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/infrastructure/skills/library.py#L52)：
  扫 `skills/*/SKILL.md`，只解析 frontmatter 的 name/description，坏的 skill 跳过不影响其它。
- `load()` [library.py:74](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/infrastructure/skills/library.py#L74)：
  读正文 + 列出同目录其它文件作为 resources。
- `read_resource()` [library.py:96](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/infrastructure/skills/library.py#L96)：
  **路径穿越防护**——`resolve()` 后必须仍在 skill 目录内，否则拒绝。
- **加 skill 无需改代码**：丢一个 `skills/<name>/SKILL.md` 进去，catalog 自动发现。

---

## 9. 文档摄入链路（离线，喂给 RAG 的数据从哪来）

**代码**：[application/ingestion_service.py](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/application/ingestion_service.py)

`POST /v1/knowledge/.../documents` → `ingest_document`：

```
content(str)
  │ parse（TextParser，markdown/text）
parsed.text
  │ Parent-Child 切分（chunker.py）
parents/children（父块给上下文，子块作检索单元）
  │ Embedding 批处理（DashScope）
ChunkRecord[]（含 title 下沉、section_path、chunk_id=<docver>:p0:c0）
  │ 写 Milvus + 标记 published
document_versions.status = published
```

- **Parent-Child 切分** [ingestion/chunker.py](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/ingestion/chunker.py)：
  按 Markdown 标题层级切父块（`section_path` 面包屑），句子感知切子块 + 同章节 overlap。
  参数父块 1500 / 子块 320 / overlap 64 token。
- **title 下沉**：文档标题写进每个 chunk（`_process` 传 `title`），修了"引用 title 恒空"的问题。
- **分片治理**：`list_chunks` + `GET /v1/knowledge/document-versions/{id}/chunks`
  能读回某版本的所有 chunk（Milvus query，`consistency_level="Strong"` 保证写后即读）。

---

## 10. 数据落到哪：MySQL 与 Milvus 的分工

### 10.1 MySQL（事务性数据）
**模型**：[infrastructure/mysql/models.py](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/infrastructure/mysql/models.py)
- 规范：UUID 存 `BINARY(16)` 用 **UUIDv7**（时间有序、索引友好）；时间 `DATETIME(6)` UTC；utf8mb4。
- 核心表：`conversations` / `messages`(含 citations JSON) / `agent_runs` / `run_events` /
  `conversation_summaries`(版本化) / `knowledge_bases` / `documents` / `document_versions` /
  `lg_checkpoints` / `outbox_events`。
- 仓储 + `SqlUnitOfWork`（`repositories.py`）：一个 UoW = 一个 session + conversations/
  messages/summaries/runs 四个仓储，事务边界由 `async with uow` + `commit()` 控制。

### 10.2 Milvus（向量检索）
**schema**：[infrastructure/milvus/schema.py](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/infrastructure/milvus/schema.py)
- collection `knowledge_chunks`：`pk`(=chunk_id) / `dense`(HNSW+COSINE) /
  `sparse`(**内置 BM25 Function + 中文 analyzer**，服务端自动生成) / `text` /
  `title` / `section_path` / `tenant_id` / `acl_tokens` / `status` / 时效字段。
- **alias `knowledge_active`**：检索查 alias，indexer 建/校验 alias——两边必须一致
  （历史上踩过：retriever 查 alias 但 indexer 没建 → 检索报 collection 不存在）。
- 召回 `retriever.py`：`build_filter_expr` 把 tenant/status/时效/acl 拼成标量表达式，
  **召回前过滤**（ACL 前置，不是召回后再滤）。
- lazy async client：`AsyncMilvusClient` 在运行循环内 lazy 创建（grpc channel 绑 loop，
  eager 建会 attach 到错的 loop）。

---

## 11. 横切关注点：追踪与配置

### 11.1 Langfuse 链路追踪
**代码**：[infrastructure/observability/tracing.py](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/infrastructure/observability/tracing.py)
- `build_trace_handler_factory`：可开关；缺 SDK/key 自动返回 no-op 工厂，**绝不影响主流程**。
- 开启时在 §3 的 `RunnableConfig.callbacks` 注入 Langfuse `CallbackHandler`
  [graph.py:384](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/agents/runtime/graph.py#L384)。
- 效果：整图 run = 一条 trace，**每个节点 + 每次 LLM 调用自动嵌套成子 span**，
  `session_id = conversation_id`。实测树形：`LangGraph → supervisor → retrieve →
  knowledge_qa → synthesize`。

### 11.2 配置
**代码**：[settings/__init__.py](file:///Users/bytedance/hzh/Agent/meituanAgent/src/sales_assistant/settings/__init__.py)
- 单层 env（Pydantic Settings），**启动冻结、无热更新**（ADR-0005）。
- 生产守卫：production 禁止 `AUTH_MODE=disabled` 和 `MODEL_PROVIDER=mock`。
- 关键项：模型网关 URL/key、检索参数(rrf_k/权重/top_k)、记忆(stm_recent_turns/trigger)、
  追踪(tracing_enabled/langfuse_*)。

---

## 12. 把整条链路串起来（一句话请求的完整旅程）

以"新商家首月的佣金政策是什么？"为例：

```
① 浏览器 POST /v1/conversations/{id}/messages，带 Idempotency-Key
② api/routes.py: 鉴权取 AuthContext → 调 ConversationService.send_message
③ send_message: 拿租约 → 幂等检查(未命中) → 事务里 bump version + 写 user 消息 + 建 run(running)
   → emit run.started → MemoryService.load_context 装配历史 → 调 AgentRuntime.run
④ AgentRuntime.run: 建 RunState + RunnableConfig(thread_id/history/callbacks) → 图.ainvoke
⑤ supervisor._classify: 近空?否 → 注入 skill catalog(level-1) → LLM 判 knowledge_qa
⑥ _route → retrieve 节点 → RetrievalService: embed → dense∥bm25(ACL前置过滤)
   → 加权RRF → rerank → Top8 Evidence → 写 state.evidence
⑦ knowledge_qa 节点: 有证据 → 编号证据入 prompt → LLM 生成 → citations 结构化 → state
⑧ synthesize → END，AgentRuntime 返回 AgentOutcome(answer + citations)
⑨ send_message: lease.ensure_valid() + lock_at_version(CAS) → 事务写 assistant 消息(含citations)
   + run→succeeded → emit message.completed → maybe_summarize(超阈值则推进摘要)
⑩ api 层把结果序列化返回；前端渲染答案 + "引用来源：[1] 佣金政策 · 首月免佣"
   （若开了 Langfuse，整个 ④-⑧ 已记录成一条嵌套 span trace）
```

---

## 13. 设计原则贯穿始终（回看全链路你会发现）

- **抽象在中心**：每层只依赖 `domain/ports` 的 Protocol，实现可换（Mock/真实）。
- **可用性优先**：rerank 挂了降级、追踪缺失 no-op、摘要失败静默、分类 LLM 挂了走启发式——
  任何非核心环节故障都不拖垮主回答。
- **多实例安全**：幂等 + 租约 + fencing + CAS + checkpoint，处处防并发和旧实例覆盖。
- **防幻觉可溯源**：证据门禁（无证据拒答）+ 编号引用 + 结构化 citations 落库。
- **省上下文预算**：RunState 只存结构化字段、skill 渐进式加载、记忆滑动窗口+摘要。

> 想看每层更细的坑位与运行方式 → `HANDOFF.md`；想看设计动机与取舍 → `specs/` 与 6 份 ADR。
