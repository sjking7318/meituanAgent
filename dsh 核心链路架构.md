# DeepSeek Harness 核心链路与架构详解

> 本文档的目标：把 `dsh` 从"一次任务进入"到"日志落盘"的**完整核心链路**讲透，覆盖每一个关键组件、每一条契约、每一个设计取舍，并给出可点击的代码坐标。
>
> 与 [学习指南.md](学习指南.md) 的分工：学习指南是"入门带路"，本文是"深入参考"。建议先读完学习指南再读本文。
>
> 阅读方式：第 1~3 章建立框架认知；第 4 章是全文核心（一次任务的完整生命周期）；第 5~9 章逐组件深挖；第 10 章是设计取舍总账。

---

## 目录

1. [定位与技术栈](#1-定位与技术栈)
2. [底座：Cordis 的三件套](#2-底座cordis-的三件套)
3. [组装模型：Profile / Bundle / Patch](#3-组装模型profile--bundle--patch)
4. [★核心链路：一次任务的完整生命周期](#4-核心链路一次任务的完整生命周期)
5. [组件深挖 A：Session 日志（唯一真相源）](#5-组件深挖-asession-日志唯一真相源)
6. [组件深挖 B：System-Prompt 组装](#6-组件深挖-bsystem-prompt-组装)
7. [组件深挖 C：Tools 执行流水线](#7-组件深挖-ctools-执行流水线)
8. [组件深挖 D：LLM 接缝与请求构建](#8-组件深挖-dllm-接缝与请求构建)
9. [Agent 句柄：创建、所有权、拦截](#9-agent-句柄创建所有权拦截)
10. [核心设计取舍总账](#10-核心设计取舍总账)
11. [附录：关键文件索引 & 术语表](#11-附录关键文件索引--术语表)

---

## 1. 定位与技术栈

**DeepSeek Harness（`dsh`）是一个 agent 运行框架**——让大模型能真正"接任务→调模型→执行工具→记录→循环"的整套外壳。它本身不是模型，模型通过能力接缝接入。

当前是**开发者预览版**（`0.1.0-rc.5`），官方明示会有破坏性变更。

| 层面 | 技术 |
|---|---|
| 核心框架 | **Cordis**（插件框架，vendored 在 [vendor/](vendor)） |
| 语言/模块 | TypeScript（`strict`）+ 全 ESM |
| 运行时 | Node.js `^22.19 \|\| >=24` |
| 包管理 | pnpm workspaces（monorepo，219 个包） |
| 前端 | React 18 + Vite 6 |
| 构建 | `tsc`（类型/lib）+ `tsdown`（打包运行时） |
| 测试 | Vitest（单测/快照/e2e）+ Playwright |

**规模实测**：219 个工作区包、1210 个 TS 源文件、65+ 个三语子系统文档、近千条设计决策记录（Agent Notes）、124 个工程门禁脚本。这是一个"治理规模 ≈ 代码规模"的项目。

---

## 2. 底座：Cordis 的三件套

理解 `dsh` 的前提是理解 Cordis。它只有三个核心概念，全挂在一个共享上下文对象 `ctx` 上：

| 概念 | 是什么 | 例子 |
|---|---|---|
| **service（服务）** | 挂在 `ctx` 上的能力对象 | `ctx.sessions`、`ctx.tools`、`ctx.systemPrompt`、`ctx.llm`、`ctx.agents` |
| **event（事件）** | 插件间通信的信号 | `agent/pre-step`、`turn/start`、`tools/execute` |
| **effect（副作用）** | 注册即登记、卸载即撤销的可逆操作 | `ctx.effect()`、`ctx.on()` 返回 disposer |

三条必须记住的 Cordis 规则（来自 `AGENTS.md`）：

1. **注册即副作用**：每个贡献都走 `ctx.effect()` / `ctx.on()`，注册表的 `register()` 返回 disposer；插件卸载时自动回滚。
2. **Waterfall 监听器必须调 `next()`**：瀑布事件的每个监听器要么返回自己的值（短路），要么调 `next()` 把控制权交给下一个。这是所有"飞行中拦截"的机制底座。
3. **可选服务用 `ctx.get(name)`**：`ctx.<name>` 只用于声明过的注入（拓扑敏感）。

**事件的三种派发模式**（关键区别）：

| 模式 | 语义 | 用途 |
|---|---|---|
| `emit` | 广播通知，无返回值 | `agent/created`、`agent/status`、`turn/start` |
| `waterfall` | 链式改写，需调 `next()` 传递，返回值权威 | `agent/pre-step`、`agent/request`、`system-prompt/assemble` |
| `serial` | 串行 await，无 `next()` | `agent/turn-stopping` |

---

## 3. 组装模型：Profile / Bundle / Patch

`dsh` 没有传统的 `main()`。运行中的它是**启动时按顺序叠加出来的一棵插件树**。

- **Bundle（捆绑包）**：一组 Cordis 配置行 + 它们挂载的代码。
  - `dsh-base`：每个 profile 的第一层——模型适配器、工具、持久化、沙箱、审批策略、设置、凭据、遥测。
  - `dsh-web-app`：加浏览器应用。
  - `dsh-headless`：加"跑一次就退出"的无服务器运行器。
- **Profile（配置档）**：一个命名组合，声明它叠哪些 bundle + 用户自己的覆盖层。内置模板：`web`、`headless`。
- **Patch（补丁层）**：按 id 定位某一行配置并整体替换，或插入新行。

**叠加顺序**（从空树开始，后层覆盖前层）：

```
各 bundle（按 profile.bundles 顺序）
  → profile 的 cordis.patch.yml
  → 家目录级 $DSH_HOME/cordis.patch.yml
  → 命令行 --patch 覆盖（可重复）
```

**看你机器实际拼出的树**（不启动）：

```sh
pnpm dsh --profile web --dump-config
pnpm dsh --profile web --dump-default-config   # 只看 bundle 层，不含用户层
```

CLI 入口的命令语法见 [apps/cli/src/args.ts](apps/cli/src/args.ts)：launcher 只解析自己的旗标（`--profile`/`--patch`/`--dump-config`），第一个它不认识的 token 起，后面全部原样交给被启动的 app。

---

## 4. ★核心链路：一次任务的完整生命周期

这是全文的核心。心脏是 [ReactLoopAgent](packages/core/agent-loop/src/agent.ts#L64)，它实现了公开的 `Agent` 契约。我们跟随"你发一句话"走完全程。

### 4.0 全景数据流

```
你的输入（Web UI / CLI / RPC）
   │  agent.followup(msg)  ← 塞进 inbox 的 next-turn 队列并唤醒
   ▼
[ inbox 收件箱 ]  两个有序队列：next-turn / next-step
   │  wakeDriver() 启动驱动 fiber
   ▼
kick() ── while(await turn()) {}          ← 一轮接一轮直到没欠债
   │
   ▼
┌──────────────────── turn()（一轮）────────────────────┐
│ session.append('turn/start', {turn})                    │
│  ┌──────────────── while (每一步) ─────────────────┐   │
│  │ preStep():                                        │   │
│  │   inbox.claim(target)          认领输入            │   │
│  │   systemPrompt.assemble()      组装提示词          │   │
│  │   waterfall 'agent/pre-step'   插件可改写/拒绝     │   │
│  │ → reject? → turn 以 blocked 结束                  │   │
│  │ session.append('step/start')                       │   │
│  │ session.append('user/message') × N                 │   │
│  │ step(assembly):                                    │   │
│  │   buildRequest()               组请求(冻结)        │   │
│  │     waterfall 'agent/request'  插件可换模型/参数   │   │
│  │     llm.prepareCall()          绑定适配器          │   │
│  │     append('request/header')   记录请求头          │   │
│  │   llm.stream(request)          流式调模型          │   │
│  │     for chunk: append('assistant/chunk')  逐片落盘 │   │
│  │   append('assistant/message')  组装完整回复        │   │
│  │   有 tool-call?                                    │   │
│  │     是 → executeToolCalls()    执行工具→回灌next-step│  │
│  │     否 → { completed }         这步收工            │   │
│  │ session.append('step/end')                         │   │
│  │ serial 'agent/turn-stopping'   插件最后可 steer    │   │
│  │ 还有 next-step 输入? 有→继续；无→break             │   │
│  └────────────────────────────────────────────────┘   │
│ session.append('turn/end', {turn, reason})              │
└──────────────────────────────────────────────────────┘
   │  inbox 还有 pending? → 开新一轮
   ▼
（全程每条 append 广播 session/event，持久化/UI/遥测各自消费）
```

### 4.1 输入进入：inbox 与三个动词

所有输入进**同一个 inbox**，区别只在"进哪个队列 + 是否唤醒驱动"。见 [agent.ts:L113-L132](packages/core/agent-loop/src/agent.ts#L113-L132)：

| 方法 | target | wakeup | 语义 |
|---|---|---|---|
| [followup()](packages/core/agent-loop/src/agent.ts#L122) | next-turn | ✅ | 新一轮任务；成为它那一轮唯一的普通消息 |
| [steer()](packages/core/agent-loop/src/agent.ts#L126) | next-step | ✅ | 转向：running 时在下一步边界被消费 |
| [inject()](packages/core/agent-loop/src/agent.ts#L130) | next-step | ❌ | 注入上下文，不唤醒；可能错过已认领批次的那次请求 |

底层统一走 [send(message, target, wakeup)](packages/core/agent-loop/src/agent.ts#L113)。有一个精妙细节：**取消后到达的唤醒输入不能加入已中止的活动**，所以会被重分类为 `next-turn`（L114-L119），等中止的活动收敛到 idle 再跑。

### 4.2 驱动的相位机（Phase）

`ReactLoopAgent` 用一个三态相位机管理生命周期，见 [Phase 定义 L38-L46](packages/core/agent-loop/src/agent.ts#L38-L46)：

```
idle         无驱动
maintenance  跑非 turn 维护任务（对外仍报 idle）
running      驱动活跃中
```

对外只暴露两个状态 `idle | running`（[status getter L99](packages/core/agent-loop/src/agent.ts#L99)），每次跨状态迁移 emit `agent/status`。`wakeDriver()`（[L172](packages/core/agent-loop/src/agent.ts#L172)）负责启动驱动或"闩锁"唤醒（maintenance/已中止时先记下，收敛后重放）。

### 4.3 一轮 turn() 的精确契约

见 [turn() L246-L330](packages/core/agent-loop/src/agent.ts#L246-L330)。关键点：

- **turn 号自增**后立刻 `append('turn/start')`（L255）。
- 第一步的 target 是 `next-turn`，之后切成 `next-step`（L261、L300）。
- **空 turn 的处理**：若 preStep 被 reject → `blocked`（L267）；若第一步认领到空消息 → `completed` 但不花模型调用（L274）。这保证"日志记录了这次尝试"。
- **max-tokens 有黏性**：一旦某步触顶，后续正常完成的步不能把 turn 结果降级（L290）。
- **闭轮条件**：`turnEnds` 已定 且 `inbox.nextStep` 为空 → 先跑 `serial 'agent/turn-stopping'`（L296）给插件最后 steer 的机会 → 再确认为空则 break（L299）。
- **turn/end 的 reason** 是结构化的 [TurnEndReason](packages/core/session/src/types.ts#L155-L177)：`completed` / `aborted` / `blocked` / `error` / `max-tokens` / `interrupted`。
- 错误处理（L302-L315）：中止 → `aborted`；`LlmError` 保留其 `failure`；其他错误压平成 `{message, code:'UNKNOWN'}`。

### 4.4 一步 step() 的精确契约

见 [step() L332-L401](packages/core/agent-loop/src/agent.ts#L332-L401)。这是燃烧室，一个 `while(true)` 循环支持请求重试：

1. **buildRequest**（L340）：组一个 `deepFreeze` 的不可变请求（详见第 8 章）。
2. **取流**（L345）：`preparedCall?.stream(request) ?? llm.stream(request)`。
3. **逐 chunk 落盘**（L347-L351）：每个 chunk 都 `append('assistant/chunk')` 并喂给 `BlockAssembler`。
4. **失败处理**（L354-L371）：流结束若是 `error`/`aborted`，跑 `waterfall 'agent/request-error'`；返回 `{kind:'retry'}` 则 `continue` 重试，否则抛 `LlmError`。
5. **组装完整回复**（L373-L390）：`append('assistant/message')`，带 `sourceEventSeqs`（引用刚才那些 chunk 的 seq）。
6. **分流**（L391-L399）：
   - `max-tokens` → 返回 `{kind:'max-tokens'}`
   - 无 tool-call → `{kind:'completed'}`
   - 有 tool-call → `executeToolCalls(...)`，结果回灌 next-step（详见第 7 章）；`concluded ? completed : null`（null 表示还欠一次请求）。

### 4.5 两条贯穿全局的铁律

**铁律 A：模型可见 ⟺ 已记录（Model-visible ⟺ logged）**

凡进入模型请求的东西必须能从日志重建。证据：
- 历史来自 [session.deriveMessages()](packages/core/agent-loop/src/agent.ts#L341)（从日志投影，不是内存变量）。
- 连流式 chunk 都落盘（L349）。
- 由每个包的运行时不变量（`invariant.ts`）强制校验。

**回报**：fork、resume、replay（无 key 快照测试）、telemetry，全是"日志的自然推论"，不需各自造轮子。

**铁律 B：新功能挂事件，不改主循环**

step/turn 里的 waterfall/serial 事件是插件的全部介入点：`agent/pre-step`（改写模型输入）、`agent/request`（改写请求配置）、`agent/request-error`（重试决策）、`agent/turn-stopping`（阻止闭轮）。加功能 = 挂监听器。

---

## 5. 组件深挖 A：Session 日志（唯一真相源）

**服务**：`ctx.sessions`。**类型定义**：[packages/core/session/src/types.ts](packages/core/session/src/types.ts)。

### 5.1 事件即真相

`Session` 是一条 **append-only 的 [SessionEvent](packages/core/session/src/types.ts#L404-L436) 日志**，是唯一真相源。模型历史由 `deriveMessages()` **投影**出来，不单独存储。每条事件带：

- `type`：判别式标签（discriminated union 的 key）
- `seq`：会话内单调递增序号
- `time`：Unix 毫秒
- `data`：按 `type` 判别的载荷
- `ignorable?`：**默认必读**——遇到不认识且无此标记的事件，读者必须**拒绝重建**而非静默丢弃（因为未知的必读事件可能改变后文解读）。

### 5.2 事件词汇表（SessionEventMap）

见 [SessionEventMap L236-L333](packages/core/session/src/types.ts#L236-L333)。核心事件：

| 事件 | 载荷要点 | 类别 |
|---|---|---|
| `turn/start` / `turn/end` | turn 号 / 结束 reason | 边界 |
| `step/start` / `step/end` | turn+step 号 | 边界 |
| `user/message` | 用户消息（真人 prompt / inject 上下文 / goal 续轮，靠 `source` 区分） | **surface** |
| `assistant/chunk` | 原始流片段（token 级回放保真） | 日志专用 |
| `assistant/message` | 组装后的完整回复，带可选 `usage` | **surface** |
| `tool/call` | `callId` + `name` + **原始未解析** arguments 字符串 | 日志专用 |
| `tool/result` | 配对 `callId` 的结果 + 可选 `error` + 可选 `meta`（工具私有展示载荷） | **surface** |
| `request/header` | 完整请求头快照（config/system/tools），日志专用，最新快照重建 | 日志专用 |
| `request/context` | 路由元数据（provider/model/contextWindow），仅变化时记 | 日志专用 |
| `todo/write` | 整个 todo 列表快照（last-write-wins） | 日志专用 |
| `session/end-seed` | 标记 seed（fork/resume 的继承前缀）结束边界 | 日志专用 |

### 5.3 Surface（有序表面）机制

只有三种事件能上"表面"（即产生模型消息）：[SurfaceEventType](packages/core/session/src/types.ts#L343-L346) = `user/message` / `assistant/message` / `tool/result`。它们携带：

- `surfaceOp`：`'append'`（追加到尾部）或 `{op:'replace', start, end}`（替换表面节点，compaction 压缩用）。
- `sourceEventSeqs`：引用的来源事件 seq（如 `assistant/message` 引用它由哪些 chunk 组成；replace 节点引用它遮蔽了哪些节点）。

这让日志不是流水账，而是**有因果链的图**：任何派生视图（模型历史、UI、遥测）都能追溯来源。

### 5.4 格式版本

[SESSION_FORMAT_VERSION](packages/core/session/src/types.ts#L56) 是单调整数，预发布期钉在 `0`：**不做兼容承诺，不兼容的日志直接拒绝，无迁移**。是否 bump 由"写入方发出什么"决定，而非"读取方能接受什么"——只有结构性变更（信封、核心语义、surface 机制）才 bump；新增普通事件类型不 bump（靠 `ignorable` 覆盖词汇增长）。

---

## 6. 组件深挖 B：System-Prompt 组装

**服务**：`ctx.systemPrompt`。**实现**：[packages/core/system-prompt/src/index.ts](packages/core/system-prompt/src/index.ts)。

### 6.1 四种可注册原料

| 方法 | 贡献 | 去向 |
|---|---|---|
| [section()](packages/core/system-prompt/src/index.ts#L381) | system 提示词的一段（带 `order`） | 拼成 system 文本 |
| [context()](packages/core/system-prompt/src/index.ts#L398) | 动态运行时信息 | 作为 user 消息进历史 |
| [tools()](packages/core/system-prompt/src/index.ts#L430) | 工具 schema provider | 组成工具列表 |
| [variable()](packages/core/system-prompt/src/index.ts#L446) | `{{变量}}` 值 | 渲染时插值 |

**关键区分**：`section` 是静态身份/人设，进 system 段；`context` 是动态快照，进 user 历史，并带前缀 "Current runtime context. This snapshot supersedes earlier runtime-context snapshots."（[L239](packages/core/system-prompt/src/index.ts#L239)）让模型知道旧快照作废。

### 6.2 section 排序约定

按 `order` 升序拼接（[L504](packages/core/system-prompt/src/index.ts#L504)）：

```
-100  harness:identity      "You are an AI agent powered by DeepSeek Harness."
   0  deployment:persona    部署方人设（可被 agent preset 同名遮蔽）
100+  工具使用指引
```

两个开场 section 在构造函数注册（[L357-L369](packages/core/system-prompt/src/index.ts#L357-L369)）。`harness:identity` 故意独立于 loop 插件（换发动机身份不丢）；[PERSONA_SECTION](packages/core/system-prompt/src/index.ts#L128) 是可覆盖槽位——同名才能替换而非重复。

### 6.3 assemble() 五步

见 [assemble() L467-L542](packages/core/system-prompt/src/index.ts#L467-L542)：

1. **解析变量**（L473-L482）：全局先、scope 链"最远先最近后"覆盖 → 最近的 scope 赢。
2. **合并 sections/contexts**（L484-L485）：scoped 同名遮蔽 global。
3. **收集工具 schema 并排序**（L487-L529）：`parameters` 做 `structuredClone` 脱钩；[orderTools](packages/core/system-prompt/src/index.ts#L164) 按 `toolOrder` 配置排，未列出的按**字典序**插到 [TOOL_ORDER_REST](packages/core/system-prompt/src/index.ts#L140) 标记位。字典序 locale 无关（[L181](packages/core/system-prompt/src/index.ts#L181)）→ 每台机器结果一致（对 KV-cache 和快照测试关键）。
4. **组出 PromptAssembly**（L519-L531）：此时未插值。
5. **跑 waterfall `system-prompt/assemble`**（L532-L535）：scope-filtered，插件最后的改写机会，返回值权威。

### 6.4 complete section 逃生舱

若某 section 标 `complete: true`（[L68-L74](packages/core/system-prompt/src/index.ts#L68-L74)），它是整个 system prompt。waterfall 照跑（让工具/上下文/变量仍解析），但跑完 [L536-L541](packages/core/system-prompt/src/index.ts#L536-L541) 强制把它恢复为唯一 section。多于一个 complete section 报错（L506）。

### 6.5 渲染与插值

[renderPrompt()](packages/core/system-prompt/src/index.ts#L212)：逐段插值 `{{变量}}` → 丢空段 → `\n\n` 拼接。[interpolate()](packages/core/system-prompt/src/index.ts#L258) 规则严格：变量名不合法/未注册/值 undefined 都**直接报错**（宁失败不静默），孤立 `{{` 当普通文本，替换值不二次扫描（防注入）。

---

## 7. 组件深挖 C：Tools 执行流水线

**服务**：`ctx.tools`。**调度器**：[packages/core/agent-loop/src/tool-calls.ts](packages/core/agent-loop/src/tool-calls.ts)。

### 7.1 独占 vs 并行的动态分组

模型一步可能调多个工具。[executeToolCalls()](packages/core/agent-loop/src/tool-calls.ts#L59) 按每个工具声明的执行模式分组（[L84-L99](packages/core/agent-loop/src/tool-calls.ts#L84-L99)）：

- **parallel**：进一个**有上限的滚动池**（`maxParallelToolCalls`）。
- **exclusive**：形成"栅栏"——单独一个一组，前面清空才能过。
- **动态重分类**：每次提交后重读后续工具的模式（L85 注释），所以运行时注册的工具能创建新栅栏。

### 7.2 乱序执行、有序提交（核心手法）

见 [runGroup()](packages/core/agent-loop/src/tool-calls.ts#L121) 与 [commitReady()](packages/core/agent-loop/src/tool-calls.ts#L146-L160)：

- 派发（dispatch）可以并发重叠。
- 但 `committed` 只沿**连续的、模型顺序的**槽位推进（L147-L149）。

**效果**：并发提速，但日志里 `tool/result` 的顺序严格等于模型看到的顺序。这对铁律 A（可重放的确定性）至关重要。

### 7.3 中断也保持日志有效

见 [L237-L242](packages/core/agent-loop/src/tool-calls.ts#L237-L242) 和 [appendSkippedToolCall()](packages/core/agent-loop/src/tool-calls.ts#L249)：

- 用户取消时，**已开始的工具先排干并按序提交**。
- **没开始的补一条合成错误结果**（`TOOL_ABORTED_BEFORE_DISPATCH`）。

为什么？每个 `tool/call` 必须有配对的 `tool/result`，否则日志残缺无法重放。这是铁律 A 逼出的严谨。

### 7.4 调度器内部失败

见 [L231-L235](packages/core/agent-loop/src/tool-calls.ts#L231-L235)：内部调度失败停止新派发，排干已派发的，用第一个失败 reject，**不伪造工具结果**（区别于用户中止的合成结果）。

### 7.5 因果链

每个 call 记住自己的 seq（[L167](packages/core/agent-loop/src/tool-calls.ts#L167)），`tool/result` 通过 `sourceEventSeqs` 指回它（[L288](packages/core/agent-loop/src/tool-calls.ts#L288)）。结果里的 `additionalContexts` 通过 `acceptContext` 回灌 next-step（L156），`concludesTurn` 决定是否结束 turn（L157）。

### 7.6 完整工具执行管线（`tools/*` 事件）

`ctx.tools` 的执行还经过一条守卫管线（架构文档描述）：`tools/pre-execute → tools/execute → tools/post-execute`，都是 waterfall。策略、审批、超时、沙箱等都挂在这里，而非改调度器。工具的 UI 渲染意图（`generic`/`terminal`/`diff`）是其设计的一部分，`presentResult` 是 `args` 的纯函数。

---

## 8. 组件深挖 D：LLM 接缝与请求构建

**服务**：`ctx.llm`。词汇（`Message`/`ContentBlock`/`StreamChunk`/请求结果）由 [packages/llm](packages/llm) 定义。

### 8.1 buildRequest 的精确流程

见 [buildRequest() L407-L495](packages/core/agent-loop/src/agent.ts#L407-L495)：

1. **确定种子配置**（L419-L437）：首个请求用 agent 的 `provider/model/maxTokens`；后续请求折叠已记录的 `request/header`（去掉适配器派生的默认值，[requestProposal L55](packages/core/agent-loop/src/agent.ts#L55)）。
2. **跑 `waterfall 'agent/request'`**（L438-L441）：插件在此换模型/调参数。缺 provider/model 报错（L443）。
3. **prepareCall 绑定适配器**（L449）：解析出精确模型的默认值。若中间件服务了未注册路由，容忍 `NO_ADAPTER`（L452-L454）。
4. **记录请求头**（L458-L470）：`canonicalHeader` 规范化，与 baseline 比较：首次记 `initial`/`resume`，变化记 `change`。
5. **记录请求上下文**（L472-L483）：provider/model/contextWindow 变化时记 `request/context`。
6. **组冻结请求**（L486-L493）：`markAgentLoopRequest(deepFreeze({...}))`，带 `messages`（=`deriveMessages()` 的投影）、`system`、`tools`、`sessionId`、`signal`。

### 8.2 为什么请求要"冻结"

`deepFreeze` 保证请求一旦构建就不可变——这是可重放性的一部分：日志记录的请求头能精确重建同一个请求。请求的 `messages` 不进 `request/header`（它们从 surface 事件投影），只有 config/system/tools 进头。

### 8.3 能力接缝的威力

`ctx.llm`、`ctx.fs`、`ctx.shell`、`ctx.subprocess` 等都是**能力接缝**（Service Definition / Provider / Consumer 三角）。换一个 provider 换全局：把 fs+subprocess 指向远程沙箱（E2B），Bash/终端/LSP 一起搬过去，无需改任何调用方。`AGENTS.md` 强制：一个角色单独存在不算 seam，加能力必须三件套齐备。

---

## 9. Agent 句柄：创建、所有权、拦截

**服务**：`ctx.agents`（[packages/core/agent/src/index.ts](packages/core/agent/src/index.ts)）。**契约**在 agent 包，**实现**在 agent-loop 包——扩展插件只依赖 `agent` 接缝，所以发动机可换。

### 9.1 创建与所有权

- `ctx.agents.create()`：新建 session + agent。
- `ctx.agents.resume()`：先载入持久化 session 再恢复。
- 返回 [AgentHandle](docs/subsystems/core.md)（`{agent, dispose}`）——`dispose` 是**能力**：只有持有者能拆这个 agent。`dispose()` 停循环 → 等退出 → 注销 → 移除 session → 回滚 scoped 世界。
- `setup(agentCtx)` 回调在两个 id 都未发布时组装 agent 的 scoped 世界；setup 失败/owner 释放 → 整个事务回滚，不发布任何 id。

### 9.2 Initiator（发起者）scope

驱动跑在 [ctx.agents.withInitiator(this, ...)](packages/core/agent-loop/src/agent.ts#L192) 里。这是**进程内因果归因**：`requireInitiator()` 在驱动下方的私有 helper 里恢复当前 agent（如工具执行 [L67](packages/core/agent-loop/src/tool-calls.ts#L67)）。规则：环境存在性既非存活证明也非授权；subject 和 owner 在 worker/进程/持久化/wire 边界仍需显式传递。

### 9.3 拦截点汇总（agent/* 事件）

| 事件 | 模式 | 作用 |
|---|---|---|
| `agent/pre-step` | waterfall | reject 或改写进入这步的消息批次 |
| `agent/request` | waterfall | 替换冻结的请求配置（不能改消息，模型可见内容必须走日志通道） |
| `agent/request-error` | waterfall | 返回 `{kind:'retry'}` 重试，`undefined` 保持终态 |
| `agent/turn-stopping` | serial | turn 将闭时，监听器可 `agent.steer()` 阻止闭轮；**数据决定，监听器顺序不影响结果** |
| `agent/session-start` | emit | session 生命周期开始，可用 `agent.inject()` 播种上下文 |
| `agent/status` | emit | idle ⇄ running |
| `agent/created` / `agent/disposed` | emit | 发布/注销 |
| `agent/error` | emit | step/turn 出错 |

`agent/turn-stopping` 的设计哲学值得注意：**用数据而非监听器顺序决定控制流**。反向控制（提前结束工具循环）也是数据——工具结果带 `concludesTurn` 即在其步结束 turn。

### 9.4 扩展模式：Map → 派生 union + 声明合并

几乎所有可扩展 sum 类型都用同一模式：一个按判别标签索引的 `…Map` 接口，用 `keyof` 派生 union；插件通过**声明合并**加变体，不用改源包。六个规范 Map：`ContentBlockMap`、`MessageSourceMap`、`FinishReasonMap`（dsh-llm），`TurnTriggerMap`、`TurnEndReasonMap`、`SessionEventMap`（dsh-session）。约定：`switch` 判别标签（不链 `if`），闭合 union 以 `assertNever` 收尾。

跨包 id 一律**品牌化**（[Branded<B>](docs/subsystems/core.md)）：结构上是 string，类型上不可互换（`SessionId` 不能传给要 `CallId` 的地方）。

---

## 10. 核心设计取舍总账

| 设计决策 | 换来什么 | 代价 | 判断 |
|---|---|---|---|
| **一切皆插件（Cordis）** | 极致可替换、可自我修改 | 无线性 main，认知陡峭，调试跨插件 | 对"给 agent 用、能自改"的目标是对的 |
| **事件溯源 + 模型可见⟺已记录** | fork/resume/replay/telemetry 白拿 | 写放大，格式版本约束 | 全项目最有价值的设计 |
| **能力接缝三角色** | 换 provider 换全局 | 单一实现时是过度设计 | 用"必须有当前 consumer"规则对冲 |
| **乱序执行、有序提交** | 工具并发提速 + 确定性可重放 | 调度器复杂 | 处理得很漂亮 |
| **严格插值、宁失败不静默** | 提示词错误早暴露 | 无 | 正确取舍 |
| **数据决定控制流（turn-stopping）** | 监听器顺序无关，可组合 | 需理解"数据即控制" | 高级设计 |
| **制度化治理（Notes/不变量/文档预算）** | 长期可维护、可被 AI 演进 | 迭代摩擦大 | 真正的护城河 |

**一句话总结**：`dsh` 是一个**用极致工程治理驯服极致插件化复杂度的 agent 运行时**。它的赌注是"可替换性 + 可重放性 + 决策留痕"三者叠加，让庞大系统长期保持可被人和 AI 共同演进。

**最值得借鉴的一点**（尤其对多 agent 编排）：**会话日志即唯一真相 + 从日志投影模型历史**。它把多 agent 系统最头疼的状态一致性、可恢复、可观测，统一成"同一个问题的三个视角"。

---

## 11. 附录：关键文件索引 & 术语表

### 关键文件（按学习优先级）

| 优先级 | 文件 | 内容 |
|---|---|---|
| ★★★ | [agent-loop/src/agent.ts](packages/core/agent-loop/src/agent.ts) | 发动机：turn/step/buildRequest |
| ★★★ | [agent-loop/src/tool-calls.ts](packages/core/agent-loop/src/tool-calls.ts) | 工具调度：乱序执行有序提交 |
| ★★☆ | [session/src/types.ts](packages/core/session/src/types.ts) | 事件词汇、surface、格式版本 |
| ★★☆ | [system-prompt/src/index.ts](packages/core/system-prompt/src/index.ts) | 提示词组装与渲染 |
| ★★☆ | [docs/subsystems/core.md](docs/subsystems/core.md) | Agent 契约、创建所有权、事件全表 |
| ★☆☆ | [docs/architecture.md](docs/architecture.md) | 架构总览（改 packages 前必读） |
| ★☆☆ | [docs/cordis-primer.md](docs/cordis-primer.md) | Cordis 框架入门 |
| ★☆☆ | [apps/cli/src/args.ts](apps/cli/src/args.ts) | CLI 命令语法 |

### 术语表

| 术语 | 一句话 |
|---|---|
| 插件 (plugin) | 往 `ctx` 贡献 service/event/effect 的最小单元 |
| ctx | 共享上下文对象，插件挂载点 |
| service | 挂在 ctx 上的能力，如 `ctx.tools` |
| event | 插件间信号；三模式：emit/waterfall/serial |
| waterfall | 链式改写事件，监听器需调 `next()` 传递 |
| effect | 可逆副作用，插件卸载自动撤销，返回 disposer |
| turn / step | 轮 / 步：一轮含多步，一步=一次模型请求+工具调用 |
| session log | append-only 事件日志，唯一真相源 |
| deriveMessages | 从日志投影出模型消息历史的函数 |
| surface | 会话表面：能产生模型消息的三类事件 |
| surfaceOp | 事件如何入表面：append / replace |
| sourceEventSeqs | 事件引用的来源事件 seq，构成因果链 |
| profile / bundle / patch | 配置档 / 捆绑包 / 补丁层 |
| capability seam | 能力接缝：Definition/Provider/Consumer 三角 |
| initiator | 进程内因果归因的发起 agent |
| persona | order=0 的人设 section，可被 preset 遮蔽 |
| branded id | 类型不可互换的 string id，如 SessionId/CallId |
| DSH_HOME | 决定 profile 与数据存放位置的环境变量 |

---

*本文档基于对 `packages/core`、`docs/subsystems/core.md` 源码与文档的实测编写，代码坐标对应当前 master 分支。项目处于预览期，接口可能变动——以源码为准。*
