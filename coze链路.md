# Coze 工作流执行引擎源码深度解析

> 本文档沉淀自一次围绕 coze 工作流后端执行引擎的连续追问式对话。
> 目标:讲清楚执行引擎的**细节与设计**,而非停留在抽象概念。
> 所有结论均基于真实源码,标注精确的文件路径与行号,可直接核对。
>
> 涉及代码基线:
> - coze 业务代码:`backend/domain/workflow/**`
> - 底层编排框架:`github.com/cloudwego/eino@v0.4.8`(Go module 缓存)

---

## 〇、阅读者提问脉络与习惯(元信息)

本节记录这次对话的提问路径,便于日后按同样的思路复盘。

### 提问路径(从宏观到微观)

```
阶段一 · 业务层认知
  1. coze 核心后端执行引擎链路
  2. coze 如何支持 skill
  3. Eino 调度器和 Runnable 到底是什么(源码角度)
  4. 一个工作流如何组织、构建、运行起来

阶段二 · 调度机制
  5. 如何推进节点运转、就绪、跳过
  6. 详细架构设计,每个部分负责什么
  7. 整体架构在做什么、用了哪些东西
  8. 如何构建运行图,coze 做了什么
  9. 谁去推进流程节点运转,如何做到,中间用了什么,各自职责

阶段三 · 细节澄清(逐行钻取)
  10. run 函数详解(graph_run.go#L107-108)
  11. L285 maxSteps 是多少,如何确定
  12. submit/wait 如何让流程流转起来(L293-300)
  13. DAG 模式没有 maxStep,节点怎么流转(L308)
  14. 流转动作在哪做(L339 isEnd)
  15. 澄清"DAG 循环 step 只有一次"的误解
  16. 如何计算下一批节点 / task 是什么 / 变量共享 / 子容器 / 为何不能循环嵌套
  17. 图构建 + 节点依赖关系,谁管理、影响什么、在哪用、如何负责
```

### 提问习惯(供后续协作对齐)

- **拒绝抽象,要具体落点**:反复要求"不要只讲原理,告诉我哪段代码、在哪一行、谁负责"。
- **要求真实源码,零编造**:强烈反感编造行号或逻辑,每个结论都要能核对到文件与行。
- **大白话优先**:要求用通俗语言解释复杂概念,严禁黑话和过度包装。
- **追踪数据结构变换**:倾向于沿着"数据从 A 变成 B"的路径建立高保真理解。
- **对系统级逻辑有强校验需求**:关注线程安全、执行隔离、并发驱动、状态一致性。
- **会基于 IDE 光标逐行深挖**:经常选中某一行,要求"这行是什么、如何确定、对当前场景是否生效"。
- **善于发现表述矛盾并追问**:会抓住前后不一致的地方(如"step 一次"vs"节点流转")要求澄清。

---

## 一、整体架构:五层分层

> 对应提问 1、7

```
┌════════════════════════════════════════════════════════════════════════════┐
║  L1  接入层 (API / Application)                          "接请求、发结果"    ║
║      HTTP/RPC handler → ApplicationService;Hertz + SSE(流式推送)          ║
╠════════════════════════════════════════════════════════════════════════════╣
║  L2  领域服务层 (Domain Service)                          "编排一次执行"     ║
║      SyncExecute / StreamExecute / Resume / AsyncExecute                    ║
╠════════════════════════════════════════════════════════════════════════════╣
║  L3  适配与构建层 (Adaptor + Compose Builder)            "画布→可执行图"     ║
║      CanvasToWorkflowSchema  +  NewWorkflow → Runnable                       ║
╠════════════════════════════════════════════════════════════════════════════╣
║  L4  调度执行层 (eino compose runtime)                    "跑 DAG"           ║
║      runner + channelManager + dagChannel + taskManager                     ║
╠════════════════════════════════════════════════════════════════════════════╣
║  L5  节点实现 + 横切能力 (Nodes + Cross-cutting)          "干具体活"         ║
║      LLM/Plugin/Knowledge/Code/HTTP/Selector/Loop/Batch/QA...               ║
║      横切: Callback事件总线、State状态机、Checkpoint、变量、Token统计        ║
╚════════════════════════════════════════════════════════════════════════════┘
```

**一句话定位这套架构在做什么**:
> 把用户在前端画的一张流程图(Canvas),翻译成一个可并发调度、可中断恢复、可流式输出、可观测的有向无环图(DAG),然后跑起来,并把每一步的过程实时落库和推送给用户。核心技术底座是 cloudwego/eino,coze 在它之上盖业务层。

**关键设计**:coze 几乎不写调度逻辑。它只把两个东西交给 eino——"节点怎么执行"(Lambda)和"分支怎么选"(condition);其余就绪判定、并发、跳过、中断全由 eino 的 `runner + channelManager + dagChannel` 完成。业务与引擎彻底解耦。

---

## 二、执行主链路(同步执行路径)

> 对应提问 1、4。入口:`backend/domain/workflow/service/executable_impl.go:52`

### 端到端 7 步

```
executable_impl.go SyncExecute:
1. Get()            从 DB 取 Workflow 实体(含 Canvas JSON)         :58
2. CanvasToSchema   画布 JSON → WorkflowSchema                       :84
3. NewWorkflow      Schema → eino 图 → Compile → Runnable            :98
4. ConvertInputs    用户输入按类型校验/转换                          :114
5. Runner.Prepare   生成 executeID、建事件通道、写 DB、起事件消费协程 :130
6. SyncRun          Runnable.Invoke → 启动 eino 调度                 :138
7. <-lastEventChan  等最终事件,组装 WorkflowExecution 返回          :150
```

### 具体示例(贯穿全文的 5 节点工作流)

```
用户输入: {"query": "帮我查北京今天的天气,结果用英文返回"}

    START ──► LLM(带天气插件) ──► Selector分支 ──┬──► HTTP(天气) ──► END
                                                   └──► Code(翻译) ──► END
```

推进时序(每个节点只执行一次,主循环转多圈):
```
step 0: submit([START])   → 完成 → 下一批=[LLM]
step 1: submit([LLM])     → 内部 ReAct: LLM→调天气API→LLM再推理 → 下一批=[Selector]
step 2: submit([Selector])→ 分支选中HTTP,跳过Code → 下一批=[HTTP]
step 3: submit([HTTP])    → 完成 → END就绪 → isEnd=true → 退出
```
**Code 节点一次也没跑**(被分支跳过)。

---

## 三、Runnable 与 Eino 调度器

> 对应提问 3

### Runnable 是什么

- **定义**:`compose/workflow.go:48`,`Runner compose.Runnable[map[string]any, map[string]any]`
- **产生**:`wf.Compile()` → `compose/workflow.go:151`
- **本质**:已经拓扑排好序、边的合并逻辑算好、检查点配置好的"DAG 可执行文件"。
- **类比**:`javac A.java → A.class` 再 `java A`。`Compile()→Runnable` = 编译链接;`Runnable.Invoke()` = 运行。

### Runnable 接口(从 coze 使用方式推导)

```go
type Runnable[I, O any] interface {
    Invoke(ctx, input I, opts ...Option) (O, error)                 // 同步:输入整体→输出整体
    Stream(ctx, input I, opts ...Option) (*StreamReader[O], error)  // 流式:输入整体→输出流
}
```
coze 三种调用:`SyncRun`(Invoke)、`AsyncRun`(safego.Go)、内部工作流嵌入(`innerWorkflowInfo.inner`)。

### "调度器"不是一个结构体

Eino **没有** `Scheduler` 结构体。所谓调度器 = **两阶段机制**:
- **静态调度(Compile 时)**:拓扑排序 + 环路检测 + 执行模式推导 + 范式能力适配。
- **动态调度(Invoke/Stream 时)**:`runner.run` 主循环 + `dagChannel` 就绪状态机。

---

## 四、图的构建:coze 做了什么 vs eino 做了什么

> 对应提问 8、17

### 核心分工

```
Canvas JSON
   │ ① 适配 CanvasToWorkflowSchema              ← coze
   ▼
WorkflowSchema { Nodes, Connections, Hierarchy }
   │ ② 建图 NewWorkflow → AddNode × N            ← coze
   │     resolveDependencies  算依赖边           ← coze
   │     New()                造节点 Lambda      ← coze
   │     AddLambdaNode/AddInput/AddBranch        ← 调 eino API
   ▼
eino Workflow(节点+边+分支挂好)
   │ ③ Compile()                                ← eino
   ▼
Runnable(拓扑排序完、可跑)
```

### coze 的三块核心代码

**(1) 依赖解析** — `compose/workflow.go:625` `resolveDependencies`

产出 `dependencyInfo`(`workflow.go:422`),把每个输入分成 4 类依赖 + 2 类非依赖:

```go
type dependencyInfo struct {
    inputs                       // 直接数据依赖(有连线+字段映射)  → 控制边+数据边
    inputsFull                   // 直接数据依赖(有连线+整包透传)  → 控制边+数据边
    dependencies                 // 纯控制依赖(有连线+不传数据)    → 只控制边
    inputsNoDirectDependency     // 间接数据依赖(无连线+引用输出)  → 只数据边
    inputsNoDirectDependencyFull //   同上,整包
    staticValues                 // 静态常量  → 编译期写死,不进依赖图
    variableInfos                // 变量引用  → 运行时前置处理器拉取,不进依赖图
    inputsForParent              // 子工作流向父级要的字段(carryOver)
}
```

分类判断(`workflow.go:672-692`):
```go
if IsInSameWorkflow(...) {
    if connMap[fromNode]存在 {      // 画布上画了线
        → inputs / inputsFull       // 直接依赖
    } else {                        // 没画线但引用了输出
        → inputsNoDirectDependency  // 间接依赖
    }
} else if IsBelowOneLevel(...) {    // 跨层:子工作流引用父节点
    → 代理到子工作流 START,路径前缀加父节点 Key,记 inputsForParent
}
```

两个自动化处理:
- `arrayDrillDown`(`workflow.go:497`):引用路径穿过数组时,自动取第一个元素(`a.b.c` 中 b 是数组 → `a.b[0].c`)。
- `merge`(`workflow.go:433`):复合节点把子节点向父级要的字段合并去重。

**(2) 节点工厂** — `compose/node_builder.go:41` `New()`

```go
func New(ctx, s *NodeSchema, inner, sc, deps, requireCheckpoint) (*Node, error) {
    // ① InputSourceAware 的节点(如 LLM)先算全量数据源
    if m.InputSourceAware { s.FullSources = GetFullSources(...) }
    // ② 主路径:节点 Configs 实现 NodeBuilder → 调 Build()
    if nb, ok := s.Configs.(schema.NodeBuilder); ok {
        n, _ := nb.Build(ctx, s, opts...)   // 每种节点自己的构建逻辑
        return toNode(s, n), nil            // 包装成 eino Lambda
    }
    // ③ 特殊:SubWorkflow 递归调 NewWorkflow(图套图)
    case NodeTypeSubWorkflow: return toNode(s, buildSubWorkflow(...)), nil
}
```

`toNode`(`node_runner.go:205`):用类型断言探测节点实现了 8 种接口中的哪几种(Invokable/Streamable/Collectable/Transformable × 带/不带 Option),统一包成 `compose.AnyLambda(invoke, stream, collect, transform)`(`node_runner.go:528`)。

**(3) 装配进图** — `compose/workflow.go:265-316` `addNodeInternal`

4 类依赖翻译成 4 个 eino API:
```go
for from := range deps.inputsFull            { wNode.AddInput(from) }                 // 控制+数据
for from, fms := range deps.inputs           { wNode.AddInput(from, fms...) }         // 控制+数据(映射)
for from, fms := range deps.inputsNoDirectDependency {
    wNode.AddInputWithOptions(from, fms, compose.WithNoDirectDependency())            // 只数据
}
for _, dep := range deps.dependencies        { wNode.AddDependency(dep) }             // 只控制
// 分支
if b := w.schema.GetBranch(ns.Key); b != nil {
    br, _ := b.GetFullBranch(ctx, bb); w.AddBranch(string(key), br)
}
// 前后处理器(状态钩子)
AddLambdaNode(key, lambda, statePreHandler(...), statePostHandler(...))
```

### eino 的 Compile

`compose/workflow.go:318` `Compile`:coze 只补两条边(START→入口、Exit→END),其余交给 eino:
- 把所有 Add 声明编译成 runner 的三张邻接表(见第六节)
- 拓扑排序 + 环路检测(`validateDAG`)
- 执行模式推导(有流式节点 → 整图 Stream)
- 上下游范式适配(上游 Stream、下游只 Invoke → 自动插 Collect)

---

## 五、依赖关系全生命周期(五个负责人)

> 对应提问 17。这是理解"节点为什么按这个顺序跑"的核心。

```
画布连线 ─①适配─► InputSources ─②解析─► dependencyInfo ─③装配─► eino邻接表 ─④编译─► dagChannel ─⑤运行─► 就绪判定
       CanvasToSchema      resolveDependencies    AddInput等API      Compile          get()
```

| 环节 | 负责人 | 位置 | 职责 |
|-----|-------|------|------|
| ① 翻译 | 适配器 | CanvasToWorkflowSchema | 画布"框和线"→每节点 InputSources |
| ② 解析 | resolveDependencies | workflow.go:625 | InputSources→4 类依赖 dependencyInfo |
| ③ 装配 | addNodeInternal | workflow.go:265 | dependencyInfo→eino Add API |
| ④ 编译 | eino Compile | graph.go | Add 声明→两张前驱邻接表 |
| ⑤ 运行 | dagChannel | dag.go | 邻接表→三态状态机,判就绪 |

### 四个 Add API 的语义(影响了什么)

| API | 建立 | 运行时影响 |
|-----|------|-----------|
| `AddInput(from, 映射)` | 控制依赖+数据依赖 | from 必须完成 + 输出按映射填我 |
| `AddInput(from)` | 控制依赖+数据依赖(整包) | 同上,整包透传 |
| `AddInputWithOptions(WithNoDirectDependency)` | **只数据依赖** | 用 from 输出,但不强制它是直接前驱(可跨分支) |
| `AddDependency(from)` | **只控制依赖** | from 先完成,但不传数据 |

### 一条依赖的一生(START → LLM,query→user_input)

```
① 画布画线 SourceKey=query, TargetKey=user_input
② LLM.InputSources = [{user_input ← START.query}]
   resolveDependencies:同层+有连线 → deps.inputs = {START:[映射 query→user_input]}
③ wNode.AddInput("START", 映射)   ← 既建控制边又建数据边
④ runner.controlPredecessors[LLM]=[START];  dataPredecessors[LLM]=[START]
⑤ LLM.dagChannel:ControlPredecessors={START:Waiting};  DataPredecessors={START:false}
   运行时 START 完成:
     reportDependencies([START]) → ControlPredecessors[START]=Ready
     reportValues({query:...})   → DataPredecessors[START]=true, Values[START]=数据
   get():控制关无 Waiting ✓ + 数据关全 true ✓ → 就绪,数据经映射变成 {user_input:...}
```

### 为什么两套依赖要分开(设计精髓)

- **控制依赖**解决"执行顺序":A 必须在 B 之前跑,不管传不传数据。
- **数据依赖**解决"跨结构引用":B 用了 A 的输出,但两者可能隔着分支——不能强制 A 是 B 的直接前驱(否则分支跳过会误伤 B),但 B 又确实要等 A 的数据。
- **收益**:同一条引用关系被拆成"等你的顺序"和"用你的数据"两个正交维度。这让 coze 能表达"跨分支引用"这种复杂场景。这正是 `resolveDependencies` 区分 `inputs` 与 `inputsNoDirectDependency`、`dagChannel` 分 `ControlPredecessors`/`DataPredecessors` 两字段的根本原因。

---

## 六、调度执行层:7 大组件职责

> 对应提问 5、6、9

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          runner (总指挥/主循环)                            │
│   graph_run.go:41  持有静态拓扑 + 编排 for 循环                            │
└───────┬───────────────────────────────────────────────┬──────────────────┘
        │ 委托"执行"                                      │ 委托"状态/数据"
        ▼                                                 ▼
┌───────────────────────────┐          ┌───────────────────────────────────┐
│  taskManager (执行器)      │          │  channelManager (数据/就绪管理器) │
│  graph_manager.go:258      │          │  graph_manager.go:114             │
└───────┬───────────────────┘          └──────┬────────────────────────────┘
        │ 每任务一个                            │ 管理 N 个
        ▼                                       ▼
┌───────────────────────────┐          ┌───────────────────────────────────┐
│  task (任务单元)           │          │  channel/dagChannel (节点状态机)  │
│  graph_manager.go:247      │          │  dag.go:50  每节点一个            │
└───────────────────────────┘          └───────────────────────────────────┘

  三个 Handler 管理器(数据变形层):
    edgeHandlerManager      graph_manager.go:39  边上数据转换(A→B 字段映射)
    preNodeHandlerManager   graph_manager.go:66  节点执行前输入变形
    preBranchHandlerManager graph_manager.go:90  分支判断前输入变形
  checkPointer:中断存档、恢复读档
```

| 组件 | 一句话职责 | 类比 |
|-----|-----------|------|
| runner | 编排主循环,定"提交→等待→算下一批"节奏 | 项目经理 |
| channelManager | 全图数据分发+依赖更新+挑就绪节点 | 调度中心/收发室 |
| dagChannel | 单节点就绪状态机,只回答"我能跑了吗" | 员工的待办清单 |
| taskManager | 把就绪节点丢并发 goroutine,跑前后处理器 | 施工队 |
| task | 一次执行的上下文数据包 | 工单 |
| 三个 Handler | 数据在边上/入节点前/入分支前的变形 | 数据转换插座 |
| checkPointer | 中断存档、恢复读档 | 存档管理员 |

---

## 七、`runner.run`:引擎总入口逐段解析

> 对应提问 10。源码:`graph_run.go:107-381`,是 Invoke 和 Stream 共用的底层实现。

### 签名(L107)
```go
func (r *runner) run(ctx, isStream bool, input any, opts ...Option) (result any, err error)
```
- `isStream`:模式开关。false=Invoke;true=Transform/Stream。`invoke()`/`transform()` 只是包一层传不同的 isStream。
- 具名返回 `(result, err)`:为了让 defer 读写它们。

### 六段式

```
段1 (L109-119) defer 收尾:兜底触发图级 OnStart/OnEnd/OnError 回调
               → coze WorkflowHandler 发 WorkflowStart/Success/Failed/Interrupt
段2 (L120-124) 选 runWrapper:runnableInvoke 或 runnableTransform
               → 一次选定贯穿全程,统一同步/流式
段3 (L126-159) 建 channelManager + taskManager + 参数校验
               → DAG 模式禁止设 maxRunSteps;extractOption 分派 DesignateNode 选项
段4 (L161-235) 检查点恢复分支(Resume 入口)
               → loadChannels 恢复 dagChannel 三态;restoreTasks 还原待跑任务
段5 (L236-273) 全新启动分支(冷启动)
               → 把 START 伪装成"已完成任务"喂给 calculateNextTasks 算第一批
段6 (L275-380) 主循环:submit → wait → 中断检测 → calculateNextTasks,直到 END/中断/取消
```

### 与 coze 的三个衔接点
1. 段5 `onGraphStart` → coze `WorkflowHandler.OnStart`
2. 段1 `onGraphEnd/onGraphError` → coze `WorkflowHandler.OnEnd/OnError`
3. 段4 `stateModifier` → coze Resume 构造的状态修改器(`workflow_run.go:178-224`)

---

## 八、推进机制:谁把节点一个个推着走

> 对应提问 9、12、13、14、15

### 核心结论
- **推进者** = 调用 `Invoke` 的那个 goroutine 里的 `runner.run` 主循环(单线程大脑)。
- **干活的** = 主循环 `go execute` 出去的子 goroutine(并发工人)。
- **心跳** = `taskManager.done` 无界 channel。

### 一拍(step)的三动作 — `graph_run.go:293-335`
```go
for step := 0; ; step++ {
    select { case <-ctx.Done(): return 取消错误; default: }  // 每拍先检查取消
    tm.submit(nextTasks)                    // 动作1:派活(并发)
    completedTasks = tm.wait()              // 动作2:等完工(阻塞在 done channel)
    // 中断检测:resolveInterruptCompletedTasks
    nextTasks, result, isEnd, _ = r.calculateNextTasks(completedTasks)  // 动作3:算下一批
    if isEnd { return result }              // END 就绪 → 收工
}
```

### submit — `graph_manager.go:282`
```go
func (t *taskManager) submit(tasks []*task) error {
    // ① 同步跑前置处理器(=coze statePreHandler,改共享 State 需顺序执行避免竞态)
    // ② 优化:留一个任务在主 goroutine 同步跑(单任务省一次 goroutine)
    // ③ 其余 go t.execute();t.num 记账(派几个 +几)
}
```

### execute(工人) — `graph_manager.go:267`
```go
func (t *taskManager) execute(currentTask *task) {
    defer func() {
        if panicInfo := recover(); ... { currentTask.err = ... }  // panic 兜住,不炸全局
        t.done.Send(currentTask)   // 无论成败,干完喊一声
    }()
    ctx := initNodeCallbacks(...)  // 触发 coze NodeHandler.OnStart/OnEnd
    currentTask.output, currentTask.err = t.runWrapper(ctx, currentTask.call.action, currentTask.input, ...)
    //                                    ↑ 真正跑 coze 节点 Lambda
}
```

### wait — `graph_manager.go:314`
```go
func (t *taskManager) waitOne() (*task, bool) {
    ta, _ := t.done.Receive()  // ★ 主循环唯一阻塞点:等工人 Send
    t.num--
    if ta.call.postProcessor != nil { 跑 postProcessor(=coze statePostHandler) }
}
// needAll(coze 非 eager)→ waitAll 循环收到 num==0 收齐
```

### 驱动装置:t.num + done channel
- `t.num`:submit 派几个 +几,wait 收几个 -几。waitAll 靠它判断"本批收齐"。
- `done`:无界 channel,工人 Send 永不阻塞。
- **流转燃料 = "节点完成"事件**:工人 done.Send 唤醒主循环 done.Receive,主循环才醒来算下一批。没有节点完成,主循环静静阻塞,不空转。

### coze 事件消费是旁路
coze 的 `HandleExecuteEvent`(`event_handle.go:697`)独立 goroutine,**不参与推进**,只被动接收回调事件写 DB + 推 SSE。推进主动权完全在 eino runner 手里。

---

## 九、就绪 / 跳过 / 流转控制(dagChannel 三态)

> 对应提问 5、13、17。核心:`dag.go`

### 每节点一个 dagChannel — `dag.go:50`
```go
type dagChannel struct {
    ControlPredecessors map[string]dependencyState // 控制前驱:三态
    Values              map[string]any             // 数据收件箱
    DataPredecessors    map[string]bool            // 间接数据前驱:是否到齐
    Skipped             bool                        // 整节点是否被跳过
}
```

### 三态 — `dag.go:42`
```go
const (
    dependencyStateWaiting dependencyState = iota // 等待中(上游没跑完)
    dependencyStateReady                          // 就绪(上游成功且选了我)
    dependencyStateSkipped                        // 跳过(上游走了别的分支)
)
```

### 就绪判定 `get()` — `dag.go:128`
```go
if ch.Skipped { return 不就绪 }
for _, state := range ch.ControlPredecessors {
    if state == dependencyStateWaiting { return 不就绪 }   // 控制关:任一 Waiting 就挡住
}
for _, ready := range ch.DataPredecessors {
    if !ready { return 不就绪 }                            // 数据关:任一未到齐就挡住
}
// 两关都过 → 就绪,取值后重置状态机(defer)
```
**就绪规则**:没被跳过 && 每个控制上游都"有结论"(Ready 或 Skipped 都算,只有 Waiting 挡住) && 所有间接数据依赖到齐。
**关键**:上游是 Skipped 也算"有结论",不阻塞下游——这是汇聚节点(如 END)不会因一条分支被跳过而卡死的根本原因。

### 三个 report 方法(状态如何变)
- `reportDependencies`(`dag.go:93`):上游成功且选中我 → Waiting→Ready
- `reportValues`(`dag.go:78`):上游把数据写进我的 Values 收件箱
- `reportSkip`(`dag.go:106`):上游走了别的分支 → Waiting→Skipped;若我所有控制上游都 Skipped → 我也 Skipped(链式传播,返回 true 触发对我的下游继续 skip)

### 分支选择 `calculateBranch` — `graph_run.go:743`
```go
for i, branch := range startChan.writeToBranches {
    ws, _ = branch.invoke(ctx, input[i])   // ★ 执行 coze 的 condition 函数
    for node := range branch.endNodes {    // 该分支所有可能终点里没被选中的 → 候选跳过
        if node 不在 ws 中 { skippedNodes[node] = ... }
    }
}
// 多分支修正:被某分支选中的,即使被另一分支丢弃也不跳过
cm.reportBranch(curNodeKey, skippedNodeList)  // 触发跳过传播
```

### 跳过传播 `reportBranch`(BFS) — `graph_manager.go:218`
```go
for _, node := range skippedNodes {
    skipped := c.channels[node].reportSkip([]string{from})  // 第一层
    if skipped { nKeys = append(nKeys, node) }
}
for i := 0; i < len(nKeys); i++ {   // 边遍历边追加 = BFS 向后传播
    for _, successor := range c.successors[nKeys[i]] {
        skipped := c.channels[successor].reportSkip([]string{nKeys[i]})
        if skipped { nKeys = append(nKeys, successor) }
    }
}
```

### coze 侧的分支条件 — `schema/branch_schema.go:125` `GetFullBranch`
```go
condition := func(ctx, in map[string]any) (map[string]bool, error) {
    // 异常优先:节点失败且配了异常分支 → 走异常端口
    if isSuccess, ok := in["isSuccess"]; ok && !isSuccess.(bool) { return bs.ExceptionMapping }
    index, isDefault, _ := extractor(ctx, in)  // Selector 判断,定端口号
    if isDefault { return bs.DefaultMapping }
    return bs.Mappings[index]                   // 命中 branch_N 端口
}
```
`extractor` 来自 `BranchBuilder.BuildBranch`(`schema/node_builder.go:69`):把节点输出映射到端口号。端口→下游的映射由 `BuildBranches`(`branch_schema.go:44`)从连线的 FromPort 构建(default / branch_error / branch_N)。

---

## 十、计算下一批节点(流转动作的落点)

> 对应提问 14、16。**所有"从完成节点→下一批节点"的流转,唯一发生在 `calculateNextTasks`。**

### `calculateNextTasks` — `graph_run.go:587`
```go
func (r *runner) calculateNextTasks(ctx, completedTasks, ...) {
    // 第1步:算完成节点指向谁(含分支)+ 分发数据
    writeChannelValues, controls, _ := r.resolveCompletedTasks(ctx, completedTasks, ...)
    // 第2步:更新下游状态 + 挑就绪
    nodeMap, _ := cm.updateAndGet(ctx, writeChannelValues, controls)
    // 第3步:END 到了没?没到就打包
    if v, ok := nodeMap[END]; ok { return nil, v, true, nil }  // isEnd
    nextTasks, _ := r.createTasks(ctx, nodeMap, optMap)
    return nextTasks, nil, false, nil
}
```

### `resolveCompletedTasks` — `graph_run.go:705`
对每个完成节点:
- (a) `controls` = 控制后继 → 谁的控制依赖该标 Ready
- (b) `calculateBranch` = 跑分支 condition,决定选中/跳过(**节点控制流转的核心**)
- (c) 输出 `copyItem` 复制 N 份 → 塞进每个后继的 `writeChannelValues[下游][我]`

### `updateAndGet` — `graph_manager.go:206`
```go
c.updateValues(ctx, values)          // → 各 dagChannel.reportValues(投数据)
c.updateDependencies(ctx, deps)      // → 各 dagChannel.reportDependencies(Waiting→Ready)
return c.getFromReadyChannels(ctx)   // 遍历全图 channel.get(),挑就绪 + preNodeHandler 变形
```

### 为什么每个节点只跑一次
- `dagChannel.get()` 取值后会重置自己状态(`dag.go:149-157` 的 defer)。
- DAG 无环,没有任何边能再把它变回就绪。
- 所以它天然只被 `createTasks` 打包一次。

### 澄清:step 多次 vs 节点一次
- **step(主循环拍数)**:多圈,有多少"层"就转多少次。
- **节点执行次数**:每个节点恰好 1 次。
- 二者不是一回事。"DAG 不需要 maxSteps"指不需要步数熔断(靠拓扑收敛自停),**不是说只转一圈**。

---

## 十一、终止条件:DAG 靠拓扑收敛而非步数

> 对应提问 11、13

### L285 的步数熔断对 coze 不生效
```go
// graph_run.go:285
if !r.dag && step >= maxSteps { return nil, ErrExceedMaxSteps }
```

### r.dag 如何确定 — `graph.go:645-653, 815-821`
```go
runType := runTypePregel                       // 默认 Pregel
if opt.nodeTriggerMode == AllPredecessor || isWorkflow(g.cmp) { runType = runTypeDAG }
if runType == runTypeDAG { validateDAG(...); r.dag = true }
```
coze 用 `compose.NewWorkflow` 建图 → `isWorkflow` 恒 true → **coze 永远是 DAG,r.dag == true**。

### maxSteps 的值 — `graph.go:840-843`
```go
if r.dag && r.options.maxRunSteps > 0 { return err("cannot set max run steps in dag mode") }
else if !r.dag && r.options.maxRunSteps == 0 { r.options.maxRunSteps = len(chanSubscribeTo) + 10 }
```
- **DAG(coze)**:maxSteps 恒为 0,且禁止设置(设了编译报错)。
- **Pregel**:默认 = 节点数 + 10。

### 两种模式语义 — `types.go:41-45`
| 模式 | 触发条件 | 允许环 | r.dag |
|-----|---------|-------|-------|
| Pregel (AnyPredecessor) | 任一前驱完成就触发 | 是 | false |
| DAG (AllPredecessor) | 所有前驱完成才触发 | 否 | true |

### 为什么 DAG 必然终止(拓扑收敛)
- 无环 + 每节点只跑一次 → 执行次数有上界。
- 单调量:全图剩余 Waiting 依赖总数。每完成一个节点就把下游对应 Waiting 改成 Ready/Skipped → Waiting 总数严格递减,有下界 0 → 必然在有限拍内耗尽。
- for 循环三个出口:`isEnd`(END 就绪)、中断(存档 return)、取消/错误。异常兜底:completedTasks 为空则报错退出(`graph_run.go:329`)。

---

## 十二、task 与 chanCall 数据结构

> 对应提问 16

### task(运行时工单) — `graph_manager.go:247`
```go
type task struct {
    ctx            context.Context  // 带 nodeKey 的上下文(回调、State 定位)
    nodeKey        string           // 跑哪个节点
    call           *chanCall        // 指向静态蓝图
    input          any              // channelManager 合并好的输入 map[string]any
    output         any              // 【执行后回填】
    option         []any            // 节点专属运行时选项(DesignateNode 产物)
    err            error            // 【执行后回填】中断错误也在这
    skipPreHandler bool             // 恢复时是否跳过前置
}
```

### chanCall(静态蓝图) — `graph_run.go:29`
```go
type chanCall struct {
    action          *composableRunnable // 真正的执行体(coze 节点 Lambda)
    writeTo         []string            // 无条件后继(普通边)
    writeToBranches []*GraphBranch      // 条件后继(分支边,condition 在这)
    controls        []string            // 控制后继
    preProcessor, postProcessor *composableRunnable // 前/后处理器(coze state 钩子)
}
```
**task = 运行时数据 + 指向静态蓝图 chanCall 的指针**。DAG 只跑一次 → 同一节点不会有两个 task。

---

## 十三、变量共享机制

> 对应提问 16。源码:`compose/state.go:357` `statePreHandlerForVars`

变量**不走 DAG 依赖边**,在节点执行前的前置处理器里动态拉取注入:
```go
switch *input.Source.Ref.VariableType {
case vo.ParentIntermediate:            // 父容器中间变量(Loop/Batch 循环变量)
    v = intermediateVarStore.Get(ctx, fromPath)
case vo.GlobalSystem, vo.GlobalUser:   // 系统/用户级全局变量
    v = varStoreHandler.Get(ctx, type, fromPath)
case vo.GlobalAPP:                     // 应用级(带进程内缓存)
    if v, ok = exeCtx.AppVarStore.Get(path); !ok {
        v = varStoreHandler.Get(...); exeCtx.AppVarStore.Set(path, v)  // 未命中才查
    }
}
nodes.SetMapValue(out, input.Path, v)  // 塞进节点输入
```

**设计要点**:
- 为什么不走依赖边?变量是跨节点跨层级的共享状态,做成 DAG 边会破坏拓扑。故 `resolveDependencies` 中凡 `VariableType != nil` 的引用直接 `continue`(`workflow.go:662-670`),记进 `variableInfos`,运行时才拉。
- App 级带进程内缓存(`exeCtx.AppVarStore`),同一次执行内多节点读同一变量只查一次。
- 三级作用域:ParentIntermediate(容器内)< GlobalAPP(应用)< GlobalSystem/User(全局)。

---

## 十四、子容器(Loop/Batch)实现

> 对应提问 16。源码:`compose/workflow.go:331` `getInnerWorkflow`

本质:**把容器内子节点单独编译成独立 inner Runnable,再把整个 inner Runnable 当"一个节点"塞进父图**。
```
Loop/Batch 节点(父)
   getInnerWorkflow:
     1. 收集 Children 子节点
     2. 裁剪 connections,只留内部相关的边       (workflow.go:339-347)
     3. 新建 inner Workflow(独立 GenState + 独立编译,共享父图 hierarchy :351)
     4. inner.Compile() → 内部 Runnable          (workflow.go:411)
   Loop.inner = 内部 Runnable                     (loop.go:41)
   运行时:Loop 节点 Lambda 每次迭代调一次 inner.Invoke/Stream(循环 N 次 = inner 跑 N 次)
```

**要点**:
- 内部图共享父图 hierarchy → 内部节点能引用父节点输出。
- carryOvers:内部节点用父级字段,通过内部 START 代理转发(inputsForParent 机制)。
- Loop 循环变量存 `IntermediateVars`(loop.go:46),即变量共享里的 ParentIntermediate 类。
- 图套图:父 DAG 的一个节点 = 一个完整子 DAG,子 DAG 有自己的 runner 和 step 循环。

---

## 十五、为什么不能循环嵌套

> 对应提问 16。两道硬约束 + 工程原因。

### 约束1:Loop/Batch 节点自己不能有父节点 — `loop.go:56-58`
```go
func (c *Config) Adapt(_, n *vo.Node, ...) (*schema.NodeSchema, error) {
    if n.Parent() != nil {
        return nil, fmt.Errorf("loop node cannot have parent: %s", n.Parent().ID)
    }
}
```
Loop 处于另一容器内(有 Parent)直接适配报错。Batch 同理。

### 约束2:内部图构建明确不处理嵌套复合节点 — `workflow.go:337-338`
```go
// trim the connections, only keep the connections that are related to the inner workflow
// ignore the cases when we have nested inner workflows, because we do not support nested composite nodes
```

### 工程原因
1. **中断恢复路径复杂度**:`workflow_run.go:182-222` 的 NodePath 恢复已需处理多层 `WrapOptWithIndex`。允许 Loop 套 Loop → 恢复路径变成 `Loop[i]→Loop[j]→Loop[k]` 笛卡尔积,DesignateOption 层级包装爆炸。
2. **状态序列化可控性**:每层容器一个独立 State + IntermediateVars,嵌套让检查点序列化层级不可控。
3. **产品语义**:嵌套循环在低代码画布难表达难调试,多数场景可用"单层循环 + 子工作流"替代。

### 重要区分
- **SubWorkflow 可以嵌套**(`buildSubWorkflow` 递归调 `NewWorkflow`)——图套图的串行调用。
- **Loop/Batch 不能嵌套**——并行迭代容器,受中断恢复复杂度限制。

---

## 十六、Skill 支持机制

> 对应提问 2。skill = Tool,在 LLM 节点内统一装配。源码:`nodes/llm/llm.go:385` `Config.Build()`

| Skill 类型 | 实现 | 位置 |
|-----------|------|------|
| 知识库(Knowledge) | RAG,Prompt 前缀注入(非 FC) | llm.go:522 |
| 插件(Plugin) | 封装成 eino InvokableTool,走 FC | llm.go:461 |
| 工作流(Workflow as Tool) | 其他 Workflow 包装成 Tool,支持嵌套 | llm.go:420 |

- **知识库**:注入 3 节点子链(意图识别模板→选库模型→检索 Lambda),检索结果拼到用户 Prompt 前缀。**不走 Function Calling**。
- **插件/工作流**:装成 Tool → 当 `len(tools)>0` 时 LLM 节点升级为 **ReAct Agent**(`react.NewAgent`,llm.go:648):LLM→FC→并行执行工具→结果回填→再推理,循环至无 tool_calls。
- **自动开检查点**(llm.go:795):用了 Plugin/Workflow Tool 的 LLM 节点必开 Checkpoint(FC 调用可中断恢复)。
- **Single Agent 内置 Skill**:`setKeywordMemory` 等,用 `## Skills Conditions` 自然语言描述让 LLM 自己决策何时调用(`singleagent/agentflow/node_tool_variables.go:62`)。

---

## 十七、中断与恢复(Checkpoint)

贯穿多处提问的横切能力。

### 中断链路
```
节点抛 InterruptRerunError
  → eino runner 捕获(resolveInterruptCompletedTasks, graph_run.go:400)
  → handleInterrupt / handleInterruptWithSubGraphAndRerunNodes(graph_run.go:446/495)
      序列化 State + channels → checkPointer.set(checkPointID)
      封装 InterruptInfo 作为 error
  → coze WorkflowHandler.OnError 捕获,递归展开嵌套/子图中断
  → SaveInterruptEvents 到 DB/Redis,状态 = Interrupted
  → SSE 推送中断 + 第一个 InterruptEvent
```

### 恢复链路 — `workflow_run.go:126-258`
```
resumeReq != nil:
  GetFirstInterruptEvent 校验 ID
  按 InterruptEvent.NodePath 构建多层 DesignateNode + WithResumeIndex(精准定位中断层级)
  GenStateModifierByEventType 注入 ResumeData
  TryLockWorkflowExecution 防并发重复恢复
  → Runnable.Invoke:eino 从 checkpoint loadChannels 恢复三态,跳过已执行节点,从中断节点重跑
```

### State 结构 — `compose/state.go:39`
```go
type State struct {
    NodeExeContexts      map[NodeKey]*execute.Context  // 节点执行上下文(检查点)
    WorkflowExeContext   *execute.Context
    ExecutedNodes        map[NodeKey]bool              // 已执行标记(防重复)
    SourceInfos          map[NodeKey]map[string]*SourceInfo // 流式溯源
    Inputs               map[NodeKey]map[string]any    // 中断时持久化的输入
    NestedWorkflowStates map[NodeKey]*NestedWorkflowState
    ResumeData           map[NodeKey]string            // 恢复输入
    IntermediateResult   map[NodeKey]map[string]any    // 分支中间结果
}
```
`state.go:51-89` 注册 30+ 类型到 eino 序列化系统,保证任意深度嵌套中断可字节级恢复。

---

## 十八、关键源码索引总表

### coze 业务代码(backend/domain/workflow)
| 主题 | 文件:行号 |
|-----|----------|
| 同步执行入口 | service/executable_impl.go:52 |
| NewWorkflow 建图 | internal/compose/workflow.go:83 |
| addNodeInternal 装配 | internal/compose/workflow.go:216 |
| 4 类依赖注入 | internal/compose/workflow.go:265 |
| AddBranch 分支注册 | internal/compose/workflow.go:296 |
| Compile → Runnable | internal/compose/workflow.go:318 |
| getInnerWorkflow 子容器 | internal/compose/workflow.go:331 |
| dependencyInfo 结构 | internal/compose/workflow.go:422 |
| resolveDependencies | internal/compose/workflow.go:625 |
| 节点工厂 New() | internal/compose/node_builder.go:41 |
| toNode(8 接口适配) | internal/compose/node_runner.go:205 |
| AnyLambda 包装 | internal/compose/node_runner.go:528 |
| statePreHandlerForVars(变量注入) | internal/compose/state.go:357 |
| State 结构 + 序列化注册 | internal/compose/state.go:39 / 51 |
| Runner.Prepare | internal/compose/workflow_run.go:107 |
| 中断恢复路径构建 | internal/compose/workflow_run.go:178 |
| BranchBuilder 接口 | internal/schema/node_builder.go:69 |
| BuildBranches(端口分类) | internal/schema/branch_schema.go:44 |
| GetFullBranch(condition) | internal/schema/branch_schema.go:125 |
| Loop 禁止有父节点 | internal/nodes/loop/loop.go:56 |
| LLM Build(skill 装配) | internal/nodes/llm/llm.go:385 |
| 事件消费主循环 | internal/execute/event_handle.go:697 |
| 三层 Callback Handler | internal/execute/callback.go:75+ |

### eino 框架(cloudwego/eino@v0.4.8/compose)
| 主题 | 文件:行号 |
|-----|----------|
| runner 结构(邻接表) | graph_run.go:41 |
| chanCall 静态蓝图 | graph_run.go:29 |
| runner.run 总入口 | graph_run.go:107 |
| 主循环 | graph_run.go:277 |
| L285 步数熔断(Pregel 专用) | graph_run.go:285 |
| 中断解析 | graph_run.go:400 |
| handleInterrupt | graph_run.go:446 |
| calculateNextTasks | graph_run.go:587 |
| createTasks | graph_run.go:612 |
| resolveCompletedTasks | graph_run.go:705 |
| calculateBranch | graph_run.go:743 |
| channel 接口 | graph_manager.go:28 |
| 三个 Handler 管理器 | graph_manager.go:39/66/90 |
| channelManager | graph_manager.go:114 |
| updateAndGet | graph_manager.go:206 |
| reportBranch(跳过 BFS) | graph_manager.go:218 |
| task 结构 | graph_manager.go:247 |
| taskManager | graph_manager.go:258 |
| execute(工人) | graph_manager.go:267 |
| submit / wait | graph_manager.go:282 / 314 |
| dagChannelBuilder | dag.go:23 |
| 三态常量 | dag.go:42 |
| dagChannel 结构 | dag.go:50 |
| reportValues/Dependencies/Skip | dag.go:78/93/106 |
| get() 就绪判定 | dag.go:128 |
| DAG vs Pregel 模式判定 | graph.go:645 / 815 |
| maxSteps 默认值 | graph.go:840 |
| NodeTriggerMode 语义 | types.go:41 |

---

## 十九、核心设计详解(10 个关键设计,逐一展开)

> 本节把每个核心结论展开为四个维度:**是什么 → 为什么这么设计 → 源码怎么落地 → 设计权衡/边界**。
> 不是速记,而是把每个设计背后的推理讲透。

---

### 设计 1:Runnable —— 编译产物,不是运行时对象

**是什么**
Runnable 是 `wf.Compile()` 的返回物(`compose/workflow.go:151`,类型 `compose.Runnable[map[string]any, map[string]any]`)。它封装了拓扑排序结果、边合并逻辑、检查点配置,对外只暴露 `Invoke`(同步)和 `Stream`(流式)两个方法。

**为什么这么设计**
把"图的定义"和"图的执行"分成两个阶段,是为了让**一次编译、多次执行**成为可能,也让编译期能做完整性校验(环路检测、类型匹配、范式适配),把错误挡在运行之前。类比 `javac A.java → A.class` 再 `java A`:Compile 是编译链接,Invoke 是运行。

**源码怎么落地**
- 定义:`Workflow` 结构体持有 `Runner compose.Runnable`(`workflow.go:48`)。
- 三种用法:`SyncRun`→`Runner.Invoke`(`workflow.go:174`);`AsyncRun`→`safego.Go` 里跑(`workflow.go:160`);子工作流→`innerWorkflowInfo.inner` 作为一个节点嵌入父图(`workflow.go:194`)。
- 同步/流式由编译期的 `wf.streamRun = sc.RequireStreaming()`(`workflow.go:93`)决定走哪个方法。

**设计权衡/边界**
编译有成本(拓扑排序、范式适配),但工作流的图结构在一次执行内不变,所以"编译一次跑到底"是划算的。中断恢复时不重新编译,而是复用同一个 Runner 从检查点续跑。

---

### 设计 2:调度器 —— 两阶段机制,而非一个结构体

**是什么**
Eino 里**没有** `Scheduler` 结构体。"调度器"是 Compile(静态规划)+ `runner.run`(动态驱动)两个阶段协作的整体机制。

**为什么这么设计**
调度的两类工作性质完全不同:拓扑排序、环路检测、模式推导是**一次性、纯计算**的,适合编译期做;就绪判定、并发派发、分支路由是**运行时、依赖实时状态**的,必须执行时做。硬拆成两阶段,各自职责单一。

**源码怎么落地**
- 静态阶段(`graph.go`):`validateDAG` 校验无环(`graph.go:816`)、`runType` 推导 DAG/Pregel(`graph.go:645`)、范式能力适配(上游 Stream+下游只 Invoke → 自动插 Collect)。
- 动态阶段(`graph_run.go:107` `runner.run`):六段式 —— defer 收尾 / 选 wrapper / 建管理器 / 恢复分支 / 冷启动 / 主循环。
- runner 持有静态拓扑(三张邻接表 `graph_run.go:41`),运行时全程不变。

**设计权衡/边界**
静态阶段能力越强,运行时越轻。代价是编译逻辑复杂(范式适配、流式推导),但换来运行时主循环极其精简(就三个动作:submit/wait/calculateNextTasks)。

---

### 设计 3:coze 与 eino 的接缝 —— 只交出两个函数

**是什么**
coze 不写任何调度逻辑,它只向 eino 提供两样东西:每个节点的**执行体 Lambda**(怎么执行)和分支节点的 **condition 函数**(怎么选下游)。就绪判定、并发、跳过、中断全由 eino 负责。

**为什么这么设计**
业务(节点种类、分支规则会频繁演进)与引擎(调度算法稳定)彻底解耦。coze 加一种新节点,只要实现 `NodeBuilder.Build` 产出 Lambda,不碰调度代码;调度算法优化,不影响业务节点。

**源码怎么落地**
- 节点执行体:`New()` 工厂(`node_builder.go:41`)→ 各节点 `Build()` → `toNode` 探测 8 种接口 → `compose.AnyLambda(invoke, stream, collect, transform)`(`node_runner.go:528`),最终塞进 `chanCall.action`。
- 分支 condition:`GetFullBranch`(`branch_schema.go:125`)产出 `func(ctx, in) (map[string]bool, error)`,由 eino 的 `calculateBranch`(`graph_run.go:743`)调用。
- 装配接缝:`addNodeInternal`(`workflow.go:265`)用 `AddInput/AddDependency/AddBranch` 把这两样喂给 eino。

**设计权衡/边界**
coze 还额外提供 pre/postProcessor(状态钩子)和三层 Callback,但这些是"挂载点"性质,不改变调度主线。真正的控制权(谁就绪、谁跑)始终在 eino。

---

### 设计 4:依赖分两套 —— 控制依赖 vs 数据依赖

**是什么**
每个节点的依赖被拆成两套独立状态:`ControlPredecessors`(控制依赖,三态)管"执行顺序",`DataPredecessors`(数据依赖,布尔)管"数据齐没齐"。就绪要求两关都过。

**为什么这么设计**
一条"引用关系"其实混着两个正交诉求:①我要等你先跑(顺序);②我要用你的输出(数据)。多数时候两者一致(有连线),但**跨分支引用**时会分裂:B 在分支外引用了分支内 A 的输出——B 确实要等 A 的数据,但**不能**把 A 设成 B 的直接控制前驱,否则一旦分支跳过 A 那侧,B 会被跳过传播误杀。拆成两套,就能表达"要你的数据,但不受你的分支跳过影响"。

**源码怎么落地**
- 解析分类(`workflow.go:672-692`):同层+有连线→`inputs`(控制+数据);同层+无连线但引用输出→`inputsNoDirectDependency`(只数据);有连线+不传数据→`dependencies`(只控制)。
- 装配成不同 API(`workflow.go:265-283`):`AddInput`(控制+数据)、`AddInputWithOptions(WithNoDirectDependency)`(只数据)、`AddDependency`(只控制)。
- 编译成两张邻接表:`controlPredecessors` / `dataPredecessors`(`graph_run.go:41`)。
- 运行时两套状态(`dag.go:23-40` dagChannelBuilder):控制前驱→`map[string]dependencyState`(三态);数据前驱→`map[string]bool`。

**设计权衡/边界**
这是整个依赖系统最精巧处。代价是解析逻辑复杂(要判断同层/跨层/有无连线),但换来对"跨分支引用""纯时序依赖"等复杂场景的准确表达。你在文档第五节看到的"一条依赖的一生"就是这套机制的完整走查。

---

### 设计 5:就绪与跳过 —— "有结论"而非"成功",跳过会传播

**是什么**
就绪 = 没被跳过 && 所有控制上游都不是 `Waiting`(Ready 或 Skipped 都算"有结论") && 所有数据依赖到齐。跳过 = 所有控制上游都 Skipped,并沿后继 BFS 链式传播。

**为什么这么设计**
关键洞察:**"跳过"也是一种"结论"**。如果就绪条件是"所有上游都成功",那分支结构里的汇聚节点(如 END,上游是互斥的两条分支)将永远无法就绪——因为总有一条分支不会成功。改成"所有上游都有结论(成功或被跳过)",汇聚节点就能在活跃分支到达后正常就绪。这是分支能收敛到 END 的根本前提。

**源码怎么落地**
- 三态定义:`dag.go:42`(Waiting/Ready/Skipped)。
- 就绪判定 `get()`:`dag.go:138-142` 只有 `state == Waiting` 才 return 不就绪 —— Skipped 不阻塞。
- 状态迁移:`reportDependencies`(→Ready,`dag.go:93`)、`reportSkip`(→Skipped,`dag.go:106`)。
- 跳过传播:`reportSkip` 返回"我是否全上游皆 Skipped",若是则 `reportBranch`(`graph_manager.go:218`)用边遍历边追加 `nKeys` 的方式 BFS 向后传播。
- 分支哪些被跳过:`calculateBranch`(`graph_run.go:743`)算出"分支终点里没被选中的",交给 `reportBranch`。

**设计权衡/边界**
"只要有一个上游不是 Skipped 就不跳过"——多分支场景下,一个节点被某分支选中、被另一分支丢弃时,不跳过(`graph_run.go:794-798` 的修正)。get() 取值后会重置状态机(`dag.go:149-157`),配合 DAG 无环,保证节点只就绪一次。

---

### 设计 6:推进动力 —— done channel 脉搏,事件驱动而非轮询

**是什么**
推进者是调用 Invoke 的那个 goroutine 里的 `runner.run` 主循环。它靠 `taskManager.done` 无界 channel 的收发脉搏一拍拍前进:submit 派活(并发)→ wait 阻塞在 `done.Receive()` → 工人干完 `done.Send()` 唤醒 → calculateNextTasks 算下一批。

**为什么这么设计**
用"节点完成事件"驱动而非定时轮询,好处是**零空转**:没有节点跑完,主循环就静静阻塞在 `done.Receive()`,不占 CPU;一有完成立刻响应。单线程主循环 + 多 goroutine 工人的模式,让"决策"串行(避免状态竞争)、"执行"并发(充分利用多核)。

**源码怎么落地**
- 主循环三动作:`graph_run.go:293`(submit)、`:298`(wait)、`:335`(calculateNextTasks)。
- 派活 `submit`(`graph_manager.go:282`):先同步跑 preProcessor(改共享 State 需串行避免竞态),再 `go t.execute`,`t.num` 记账。留一个任务在主 goroutine 同步跑(单任务省一次 goroutine)。
- 工人 `execute`(`graph_manager.go:267`):`defer recover`(一个节点 panic 不炸全局)+ `defer t.done.Send`(无论成败必汇报)+ `initNodeCallbacks`(触发 coze NodeHandler)。
- 收活 `wait/waitOne`(`graph_manager.go:314`):`done.Receive` + `t.num--`,顺手跑 postProcessor。

**设计权衡/边界**
`t.num`(计数器)+ `done`(无界 channel)是配对装置:num 保证收齐,无界保证工人 Send 永不阻塞。coze 事件消费 goroutine(`event_handle.go:697`)是**旁路观察者**,只写 DB/推 SSE,不参与推进,推进主动权完全在 eino。

---

### 设计 7:流转控制的唯一落点 —— calculateNextTasks

**是什么**
"从完成节点 → 下一批节点"这个转换,唯一发生在 `calculateNextTasks`(`graph_run.go:587`)。分支决策在它内部的 `resolveCompletedTasks → calculateBranch`。

**为什么这么设计**
把"流转决策"收敛到一个函数,主循环只管"派活/等活"的节奏,不掺和"下一个是谁"的逻辑。职责单一,便于理解和维护——想搞清楚流转,只看这一个函数即可。

**源码怎么落地**
三步(`graph_run.go:587-610`):
1. `resolveCompletedTasks`(`:705`):对每个完成节点算控制后继(controls)、跑 `calculateBranch` 定分支选中/跳过、把输出 `copyItem` 复制 N 份塞进下游收件箱。
2. `updateAndGet`(`graph_manager.go:206`):`reportValues`(投数据)+ `reportDependencies`(标 Ready)+ `getFromReadyChannels`(遍历全图 `get()` 挑就绪)。
3. `createTasks`(`:612`):把就绪节点打包成 nextTasks;若 `nodeMap[END]` 存在则 isEnd=true 收工。

**设计权衡/边界**
数据分发用 `copyItem`(`graph_run.go:891`):一个节点有 N 个后继就复制 N 份输出(流式则 stream.copy),避免多个下游共享同一份数据引发竞争。

---

### 设计 8:执行次数 —— step 多次,每节点恰好一次

**是什么**
主循环 `for step` 会转多圈(有多少"层"就转多少次),但每个节点在整个执行中只被执行一次。这两个"次数"不是一回事。

**为什么这么设计**
DAG(AllPredecessor 语义)天然表达"一次性数据流管道":每个节点等齐所有输入、算一次、输出给下游,不需要也不应该重复执行。这与 Pregel(允许环、节点可多次触发)是两种模型。coze 选 DAG,因为工作流就是数据管道,不是迭代计算。

**源码怎么落地**
- 每节点只跑一次的保证:`dagChannel.get()` 取值后重置状态(`dag.go:149-157`)+ DAG 无环 → 没有边能让节点二次就绪 → 只被 createTasks 打包一次。
- 主循环无终止条件 `for step := 0; ; step++`(`graph_run.go:277`),靠 isEnd/中断/取消三个出口退出。
- 为什么不需要 maxSteps:见设计 9(拓扑收敛)。

**设计权衡/边界**
澄清常见误解:"DAG 不需要 maxSteps"指不需要步数熔断(靠拓扑收敛自停),**不是说只转一圈**。5 节点线性图会转约 4~5 圈 step,但每节点各跑一次。

---

### 设计 9:终止保证 —— 拓扑收敛,而非步数限制

**是什么**
coze 是 DAG 模式(`r.dag=true`),`maxSteps` 恒为 0 且禁止设置。主循环靠图的拓扑结构必然收敛而终止,不靠步数上限。`graph_run.go:285` 的步数熔断(`if !r.dag && step >= maxSteps`)对 coze 永不生效,它是 Pregel 专用的防死循环保险丝。

**为什么这么设计**
DAG 无环 + 每节点只跑一次,执行次数天然有上界,不可能无限循环,所以步数熔断是多余的——设了反而可能误伤合法的大图。而 Pregel 允许环,可能无限转,必须有熔断。两种模式区别对待。

**源码怎么落地**
- 模式判定:`isWorkflow(g.cmp)` 为 true → DAG(`graph.go:652`);coze 用 `compose.NewWorkflow` 建图故恒为 DAG。
- maxSteps 规则:`graph.go:840` —— DAG 设了 maxSteps 直接编译报错;Pregel 默认 = 节点数 + 10。
- 收敛的数学保证:单调量 = 全图剩余 Waiting 依赖总数。每完成一个节点就把下游对应 Waiting 改成 Ready/Skipped → Waiting 总数严格递减,有下界 0 → 必然有限拍内耗尽。
- 异常兜底:某拍 completedTasks 为空(理论不该发生)则报错退出(`graph_run.go:329`)。

**设计权衡/边界**
三个正常出口:isEnd(END 就绪)、中断(存档 return)、取消/超时(每拍开头 `<-ctx.Done()`)。DAG 的"必然终止"是编译期 `validateDAG` 保证无环换来的。

---

### 设计 10:变量共享 与 容器嵌套限制

**变量共享 —— 旁路注入,不进依赖图**

- **是什么**:变量(Global/App/User/ParentIntermediate)不走 DAG 依赖边,在节点执行前的前置处理器 `statePreHandlerForVars`(`state.go:357`)里动态拉取注入。
- **为什么**:变量是跨节点、跨层级的共享状态。若做成 DAG 边,会在拓扑图里引入大量跨层连接,破坏 DAG 结构、干扰就绪判定。旁路注入让变量与拓扑解耦。
- **源码**:`resolveDependencies` 中凡 `VariableType != nil` 的引用直接 `continue`(`workflow.go:662-670`),记进 `variableInfos`;运行时 `statePreHandlerForVars` 按类型分发拉取。App 级带进程内缓存(`exeCtx.AppVarStore`,`state.go:409-416`),同一次执行多节点读同一变量只查一次存储。
- **作用域**:ParentIntermediate(容器内)< GlobalAPP(应用)< GlobalSystem/User(全局)。

**子容器实现 —— 图套图**

- **是什么**:Loop/Batch 把内部子节点单独编译成独立 inner Runnable,当"一个节点"塞进父图,迭代时反复 Invoke。
- **源码**:`getInnerWorkflow`(`workflow.go:331`)裁剪内部连线、新建独立 GenState 的 inner Workflow、`inner.Compile()`;`Loop.inner` 持有内部 Runnable(`loop.go:41`);共享父图 hierarchy(`workflow.go:351`)使内部节点能引用父节点输出;carryOvers 通过内部 START 代理转发父级字段。

**为什么 SubWorkflow 能嵌套,Loop/Batch 不能**

- **SubWorkflow 可嵌套**:`buildSubWorkflow` 递归调 `NewWorkflow`(`node_builder.go:102`),是图套图的串行调用,恢复路径线性可控。
- **Loop/Batch 不可嵌套**——两道硬约束:
  1. Loop 节点禁止有父节点:`loop.go:56-58`,有 Parent 直接适配报错。
  2. 内部图构建明确不处理嵌套复合节点:`workflow.go:337-338` 注释 "we do not support nested composite nodes"。
- **工程原因**:
  1. **中断恢复路径复杂度**:`workflow_run.go:182-222` 的 NodePath 恢复已需多层 `WrapOptWithIndex`。Loop 套 Loop → 恢复路径变成 `Loop[i]→Loop[j]→Loop[k]` 笛卡尔积,DesignateOption 层级包装爆炸。
  2. **状态序列化可控性**:每层容器一个独立 State + IntermediateVars,嵌套让检查点序列化层级不可控。
  3. **产品语义**:嵌套循环在低代码画布难表达难调试,多数场景可用"单层循环 + 子工作流"替代。

**设计权衡/边界**
这是 coze 在灵活性与可维护性之间画的明确的线:并行迭代容器(Loop/Batch)限制单层,串行调用容器(SubWorkflow)允许嵌套。本质是"图套图 + 中断恢复"的组合复杂度必须有上限。

---

*本文档由对话沉淀整理,所有行号基于当前代码基线,如源码演进请以实际为准。*
