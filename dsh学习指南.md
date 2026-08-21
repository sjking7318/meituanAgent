# DeepSeek Harness 小白学习指南

> 面向零基础读者的入门教程。目标：读完后你能说清楚"这个项目是什么、怎么跑起来、代码的心脏在哪、一个任务是如何被执行的"，并知道接下来该读哪些文件。
>
> 阅读顺序建议：从上往下。每一章末尾有【动手】小任务，边读边验证。

---

## 第 0 章：先建立一个心智图

想象你雇了一个"AI 员工"帮你写代码。它要能：接任务 → 想清楚要做什么 → 真的去读文件、跑命令、查网页 → 把过程记下来 → 继续下一步，直到完成。

**DeepSeek Harness（简称 `dsh`）就是这个"AI 员工"的整套工作台**。它本身不是大模型，而是让大模型能真正"干活"的外壳（英文叫 harness，马具/挽具的意思——套在模型身上让它拉车）。

一句话定位：

> `dsh` = 接任务 + 组装提示词 + 调模型 + 执行工具 + 记录全过程 的循环引擎。

它现在是**开发者预览版**，官方明说会有破坏性改动，所以别把任何 API 当成稳定的。

---

## 第 1 章：唯一必须先懂的思想——"一切皆插件"

这是理解整个项目的钥匙，务必记牢。

`dsh` 建在一个叫 **Cordis** 的框架上。Cordis 的世界观是：

> 有一个共享的"上下文对象" `ctx`。所有功能都是**插件**，插件往 `ctx` 上贡献三样东西：
> 1. **服务（service）**——比如 `ctx.sessions`（会话日志）、`ctx.tools`（工具箱）
> 2. **事件（event）**——比如"一步开始了""模型要发请求了"，别的插件可以监听
> 3. **可逆的副作用（effect）**——注册就登记，插件卸载时自动撤销

**最反直觉、也最重要的一点**：这里**没有"内核"可以改**。模型适配器是插件、工具箱是插件、会话日志是插件，甚至那个"接任务→调模型→执行工具"的**主循环本身也是插件**。

所以扩展 `dsh` 的方式不是"改核心代码"，而是"**在旁边挂一个新插件**"。

打个比方：别的软件像一栋楼，改功能要砸墙。`dsh` 像乐高，加功能就是再拼一块，拆掉也不留痕。

**新手记住这句话就够了**：*看到任何功能，先问"它是哪个插件贡献的？"*

---

## 第 2 章：把它跑起来

环境要求：Node.js（`^22.19` 或 `>=24`）、pnpm。

三步启动（在仓库根目录）：

```sh
pnpm install      # 1. 装依赖（约 2 分钟）
pnpm run build    # 2. 编译：TypeScript → lib，再打包 Web 前端
pnpm dsh web      # 3. 启动 Web UI
```

启动成功会打印：`dsh web: http://127.0.0.1:3080`

然后在浏览器里：

1. 打开 `http://127.0.0.1:3080`
2. **Settings → Models**：填入 DeepSeek API key（实时生效，不用重启）
3. **Choose workspace**：选一个项目目录（不选的话输入框是灰的，发不了消息）
4. 开一个会话，发一句："总结这个仓库，列出主要的包"

### 踩坑提示（本机实测）

`dsh` 默认把数据写到家目录的 `~/.dsh`。如果你的运行环境（比如受限沙箱）禁止写家目录，会报 `EPERM ... mkdir '/Users/.../.dsh/...'`。解决办法是把数据目录指到工作区内：

```sh
DSH_HOME=<仓库路径>/.dsh-home pnpm dsh web
```

`DSH_HOME` 这个环境变量决定了 profile 和数据存哪里。

【动手】把服务跑起来，访问 3080 端口看到界面。

---

## 第 3 章：启动时是怎么"拼"出来的——Profile 与 Bundle

你运行的 `dsh` 是**启动时按顺序叠加的一棵插件树**。两个概念：

- **Bundle（捆绑包）**：一组"插件 + 它们的配置"。最基础的是 `dsh-base`（模型适配器、工具、持久化、沙箱、审批策略、设置等），它是每个组合的**第一层**。`dsh-web-app` 再加上浏览器界面；`dsh-headless` 加上"跑一次就退出"的命令行模式。
- **Profile（配置档）**：一个命名的组合，规定"我要叠哪几个 bundle" + 用户自己的覆盖层。内置两个：`web` 和 `headless`。

叠加顺序（从空树开始，后面的能覆盖前面的）：

```
各 bundle（按 profile 里列的顺序）
  → profile 自己的 cordis.patch.yml
  → 家目录级的 cordis.patch.yml
  → 命令行 --patch 覆盖
```

想看你机器**实际拼出来的树**（不真的启动）：

```sh
pnpm dsh --profile web --dump-config
```

【动手】跑一下 `--dump-config`，感受"整个产品就是一份可配置的插件清单"。

---

## 第 4 章：代码的心脏——一个任务是怎么被执行的

这是全项目最核心的部分。心脏是一个类：

📍 [`ReactLoopAgent`](packages/core/agent-loop/src/agent.ts) —— "ReAct 循环"的驱动器（发动机）。

它做的事一句话概括：**不断地"开一轮 → 认领输入 → 组提示词 → 调模型 → 执行工具 → 记日志"，直到没有欠债。**

### 4.1 两个核心术语

- **step（步）** = 一次模型请求 + 这次请求触发的工具调用。
- **turn（轮）** = 0 到多个 step。有输入进来时开启，做完没有"欠债"（没有待处理的工具/输入）时关闭。

### 4.2 一轮（turn）的生命周期

看 `turn()` 方法（`packages/core/agent-loop/src/agent.ts`），流程就是：

```
turn/start                       ← 记一条"开始"日志
  循环：
    组装提示词 + 认领输入          ← preStep()
      ├─ 被拒 → 本轮以 blocked 结束
      └─ 通过 → 进入这一步
    step/start                   ← 记日志
    把用户消息写进日志
    step(...)                    ← 真正调模型、执行工具（见下）
    step/end                     ← 记日志
    agent/turn-stopping          ← 一个可被插件拦截的"要停了"事件
    还有下一步输入吗？有→继续，没有→跳出
turn/end                         ← 记一条"结束"日志（带结束原因）
```

### 4.3 一步（step）里发生什么——发动机的燃烧室

看 `step()` 方法：

```
1. buildRequest(...)              组一个"冻结"的请求（不可再改）
2. llm.stream(request)            流式调用模型
3. for 每个 chunk:                每个流片段都写进日志！
     append('assistant/chunk')
4. 组装完整回复 → append('assistant/message')
5. 模型有没有要调工具？
     没有 → 这步完成
     有   → executeToolCalls(...) 执行工具，结果塞回下一步
```

### 4.4 两条必须理解的"铁律"

**铁律 A：模型看到的 ⟺ 日志里记的（Model-visible ⟺ logged）**

凡是进入模型请求的东西，都必须能从会话日志重建。证据：发请求时用的历史不是内存里攒的，而是 `this.session.deriveMessages()` **从日志投影**出来的；连每个流式 chunk 都落盘。

*为什么重要*：正因如此，会话可以被 fork（分叉）、resume（恢复）、replay（回放）、做遥测——因为**日志就是唯一真相**。

**铁律 B：新功能挂在事件上，不改主循环**

循环里有几个 `waterfall` 事件（瀑布流事件），是插件介入的口子：

- `agent/pre-step`——决定模型这一步能看到什么（可改写、可拒绝）
- `agent/request`——可改写请求配置（换模型、调参数）
- `agent/request-error`——请求失败后，决定要不要重试

**waterfall 语义**：每个监听器必须调用 `next()` 才会把控制权传给下一个；不调就"短路"。这是 `dsh` 所有"飞行中拦截"能力的底层机制。

### 4.5 输入怎么进来——一个收件箱，三个动词

所有输入都进同一个"收件箱（inbox）"，区别只在"放进哪个队列 + 要不要立刻唤醒"：

| 方法 | 含义 | 场景 |
|---|---|---|
| `followup()` | 开新一轮 | 用户发来新任务 |
| `steer()` | 插到下一步，立刻改方向 | 中途"等一下，改成……" |
| `inject()` | 塞进下一步，但不唤醒 | 悄悄补充上下文，等下次请求捎带 |

【动手】打开 `packages/core/agent-loop/src/agent.ts`，对照 4.2/4.3 把 `turn()` 和 `step()` 读一遍。这是全项目性价比最高的一次阅读。

---

## 第 5 章：提示词是如何构建的

提示词**不是一段写死的字符串**，而是多个插件贡献片段、按序组装、最后插值渲染出来的。掌管者是 `ctx.systemPrompt` 服务（`packages/core/system-prompt/src/index.ts`）。

### 5.1 插件能贡献四种"原料"

| 方法 | 贡献什么 | 去向 |
|---|---|---|
| `section()` | system 提示词的一段（带 `order` 排序） | 拼成 system 文本 |
| `context()` | 动态运行时信息（如当前工作区、时间） | 作为一条 user 消息进历史 |
| `tools()` | 工具的 schema | 组成工具列表给模型 |
| `variable()` | `{{变量}}` 的值 | 渲染时替换 |

**关键区分**：`section` 是静态的身份/人设，进 system 段；`context` 是动态快照，进 user 消息历史（并带一句"本快照取代之前的快照"，让模型知道旧的作废）。

### 5.2 section 的排序约定（提示词骨架）

sections 按 `order` **升序拼接**：

```
order = -100  →  harness 身份："You are an AI agent powered by DeepSeek Harness."
order =  0    →  部署方人设（persona）——一个可被覆盖的槽位
order = 100+  →  工具使用指引
```

`order=0` 的人设槽位很巧妙：agent preset 可以注册**同名** section 来"遮蔽"默认人设——同名才能替换而不是重复。

### 5.3 组装 5 步（`assemble()`）

发动机每步之前调用它：

```
1. 解析变量         （更近的作用域覆盖更远的）
2. 合并 sections/contexts（scoped 遮蔽 global）
3. 收集工具 schema 并排序（默认字典序，locale 无关 → 每台机器结果一致）
4. 组出 PromptAssembly（此时还没插值）
5. 跑 'system-prompt/assemble' 瀑布事件（插件最后的改写机会）
```

### 5.4 渲染成最终字符串

`renderPrompt()`：逐段插值 `{{变量}}` → 丢掉空段 → 用空行拼接。插值规则很严格：变量名不合法、未注册、值为 undefined 都**直接报错**（宁可失败也不静默出错），且替换进来的值不会被二次扫描（防注入）。

【动手】读 `packages/core/system-prompt/src/index.ts` 的 `assemble()` 和 `renderPrompt()` 两个函数。

---

## 第 6 章：能力接缝（Capability Seam）——为什么"换一个零件换全局"

这是 `dsh` 架构的精髓设计。一个"能力"由**三个角色**组成：

1. **Service Definition（定义）**——声明接口，"我能做什么"
2. **Service Provider（提供者）**——具体实现，"我怎么做"
3. **Consumer（消费者）**——使用它的人，通常是一个给模型用的工具

**威力在哪**：换一个 provider 就能改变整个产品行为。比如把"文件系统"和"子进程"这两个 provider 从本地换成远程沙箱，那么 Bash、终端、LSP（语言服务）会**一起**跟着搬过去，因为它们共用同一个"执行世界"——不用改任何调用方。

新手判断题：*看到一个功能，能分出它的"接口/实现/使用者"三层吗？* 能，你就抓住了这个项目的设计手感。

---

## 第 7 章：仓库地图（该去哪找东西）

```
vendor/       Cordis 框架源码副本（锚定上游版本）
packages/     所有 @deepseek-ai/dsh-* 包，按功能分组：
  core/         产品骨架：session / system-prompt / tools / agent / agent-loop ★心脏在这
  llm/          大模型能力：消息与流的词汇 + DeepSeek 适配器
  shell/        bash 执行能力
  subprocess/   子进程能力
  terminal/     常驻终端
  fs/           文件系统能力 + 策略
  lsp/          语言服务器能力
  web/          联网搜索/抓取能力
  subagent/     子 agent 委派能力
  skill/        技能（可复用工作流）注册与加载
  bundle/       可安装的 bundle（base / web-app / headless）
  ...           todo / plan / compaction / hooks / session / settings 等
apps/
  cli/          dsh 命令行启动器（入口 src/bin.ts，命令语法 src/args.ts）
  web/          Web 前端
docs/         架构、子系统、教程、cookbook（改 packages 前先读 docs/architecture.md）
examples/     可运行的 cordis.yml 示例
python/       Python SDK
scripts/      各种校验/生成脚本（doc-sync、lint、catalog 生成等）
```

**新手最该先读的三个文件**（按顺序）：

1. `README.md` —— 项目是什么、怎么跑
2. `docs/architecture.md` —— 架构总览（改 `packages/` 前必读）
3. `packages/core/agent-loop/src/agent.ts` —— 代码的心脏

---

## 第 8 章：推荐学习路径（循序渐进）

**第 1 周·跑通 + 全局观**
- [ ] 按第 2 章跑起来，Web UI 发几个任务
- [ ] 读 `README.md` + `docs/architecture.md`
- [ ] 跑 `pnpm dsh --profile web --dump-config` 看插件树

**第 2 周·读懂心脏**
- [ ] 精读 `agent-loop/src/agent.ts` 的 `turn()` 和 `step()`
- [ ] 理解两条铁律（模型可见⟺已记录；新功能挂事件）
- [ ] 读 `agent-loop/src/tool-calls.ts`——工具怎么执行、结果怎么回灌

**第 3 周·理解数据与能力**
- [ ] 读 `core/session`——日志如何 `deriveMessages()` 投影出模型历史
- [ ] 读 `core/system-prompt`——提示词组装（第 5 章）
- [ ] 挑一个能力接缝（`fs` 或 `shell`）看它的"定义/提供者/消费者"三层

**第 4 周·动手扩展**
- [ ] 照着 `docs/cookbook/adding-a-tool.md` 加一个自己的工具
- [ ] 照着 `docs/cookbook/adding-a-package.md` 加一个自己的包

---

## 第 9 章：常见疑问速答（FAQ）

**Q：`dsh` 和大模型是什么关系？**
A：`dsh` 是外壳，模型是大脑。模型通过 `ctx.llm` 这个能力接缝接入，DeepSeek 是其中一个 provider，也可以接别的 OpenAI 兼容端点。

**Q：我想加个新功能，从哪下手？**
A：先问"它属于哪种扩展点"。加模型→注册到 `ctx.llm`；加工具→注册到 `ctx.tools`；想拦截请求/工具/轮次→监听对应的 `agent/*` 或 `tools/*` 事件。**基本不需要改主循环。**

**Q：为什么什么都要写进日志？**
A：因为"日志是唯一真相"（铁律 A）。fork、恢复、回放、遥测全靠它。任何模型能看到的东西，必须能从日志重建。

**Q：`profile` 和 `bundle` 有啥区别？**
A：bundle 是"一包插件+配置"；profile 是"我要叠哪几个 bundle 的组合"。profile 由多个 bundle 层叠而成。

**Q：出错了怎么排查？**
A：先看启动日志；用 `--dump-config` 确认插件树；权限类报错优先怀疑 `DSH_HOME` 指向了受限目录。

---

## 术语小抄

| 术语 | 一句话 |
|---|---|
| 插件 (plugin) | 往 `ctx` 贡献服务/事件/副作用的最小单元；一切皆插件 |
| ctx | 共享上下文对象，插件挂载点 |
| service | 挂在 ctx 上的能力，如 `ctx.tools` |
| event | 插件间通信的信号；waterfall 事件需调 `next()` 传递 |
| effect | 可逆副作用，插件卸载自动撤销 |
| turn / step | 轮 / 步：一轮含多步，一步=一次模型请求+工具调用 |
| session log | append-only 事件日志，唯一真相源 |
| profile / bundle | 配置档 / 捆绑包：profile 由多个 bundle 叠成 |
| capability seam | 能力接缝：定义/提供者/消费者 三角，换 provider 换全局 |
| persona | order=0 的人设 section，可被 preset 遮蔽覆盖 |

---

祝学习顺利。有任何一章想让我展开成"带代码逐行讲解"的深入版，告诉我章节号即可。
