# Graphon 引擎核心流转与组件运行详解

> 本文聚焦 Graphon 的**执行引擎（graph_engine）**：一张图从被点火到跑完，中间的组件如何协作、
> 状态如何流转、节点如何执行、事件如何消费、上下游如何判定、扩展节点如何接入。
> 所有结论都标注了 `文件:行号` 作为证据锚点，便于对照源码。

---

## 目录

1. [先建立心智模型：五层职责](#1-先建立心智模型五层职责)
2. [三种"上下文"——最容易混的概念](#2-三种上下文最容易混的概念)
3. [核心状态载体：数据到底存在哪](#3-核心状态载体数据到底存在哪)
4. [三线程 + 双队列：整体运行骨架](#4-三线程--双队列整体运行骨架)
5. [装配期：GraphEngine 如何把组件织成一张网](#5-装配期graphengine-如何把组件织成一张网)
6. [点火：第一个任务如何入队](#6-点火第一个任务如何入队)
7. [节点执行机制：生成器、事件流、Worker](#7-节点执行机制生成器事件流worker)
8. [事件消费与图推进：Dispatcher 与 EventHandler](#8-事件消费与图推进dispatcher-与-eventhandler)
9. [上下游流转判定：边三态与就绪算法](#9-上下游流转判定边三态与就绪算法)
10. [多入边节点如何确定下来](#10-多入边节点如何确定下来)
11. [一次完整执行的时间轴（start → llm → answer）](#11-一次完整执行的时间轴start--llm--answer)
12. [扩展节点之一：插件类节点（tool / llm）](#12-扩展节点之一插件类节点tool--llm)
13. [扩展节点之二：容器类节点（loop / iteration）](#13-扩展节点之二容器类节点loop--iteration)
14. [事件的流式输出与整理（filters 层）](#14-事件的流式输出与整理filters-层)
15. [暂停与恢复](#15-暂停与恢复)
16. [关键设计思想总结](#16-关键设计思想总结)
17. [组件能力速查表](#17-组件能力速查表)

---

## 1. 先建立心智模型：五层职责

Graphon 引擎里所有的类，都可以归进下面五层。看到任何一个陌生的类，先问自己"它属于哪一层"，就不会在密集的相互调用里迷路。

```
┌─ 编排层（谁在驱动）──────  GraphEngine · Dispatcher · WorkerPool · Worker
├─ 状态层（存什么）────────  GraphRuntimeState · GraphExecution · NodeExecution · VariablePool
├─ 推进层（怎么往前走）────  GraphStateManager · EdgeProcessor · SkipPropagator
├─ 帧/容器层（子图隔离）──  ExecutionFrame · FrameRegistry · ContainerHandler
└─ 事件层（怎么沟通）──────  EventManager · EventHandler · 各类 Event
```

一句话概括各层：

- **编排层**：驱动整个执行，管线程和调度。
- **状态层**：承载所有运行数据，是各组件共享的"账本"。
- **推进层**：核心算法，负责"某节点跑完后，下一步该跑谁"。
- **帧/容器层**：把"一套图执行环境"打包，让子图能递归复用主图机制。
- **事件层**：所有组件之间只通过"事件"通信，彻底解耦。

理解全局的关键洞察：**Worker 只负责"执行"，Dispatcher 单线程负责"决策/推进"；所有对图状态的写操作集中在 Dispatcher 一个线程里，因此天然无需加锁。**

---

## 2. 三种"上下文"——最容易混的概念

Graphon 里有三种不同生命周期的"上下文"。分清它们是理解全局的钥匙。

| 上下文 | 类 | 生命周期 | 特性 | 装什么 |
|---|---|---|---|---|
| **静态上下文** | `GraphInitParams` | 整个执行不变 | **只读** | workflow_id、graph_config、run_context、call_depth |
| **运行时上下文** | `GraphRuntimeState` | 整个执行可变 | **可变、全局共享** | 变量池、队列、token 用量、outputs、执行状态机 |
| **数据上下文** | `VariablePool` | 整个执行可变 | 键值存储 | 节点间传递的变量 `(node_id, var_name) → value` |

证据在两个类的文档注释里说得很清楚：

- `GraphInitParams`（`src/graphon/entities/graph_init_params.py:9-19`）：明说 "encapsulates the configurations and contextual information that **remain constant** throughout a single execution"。注意它对"单次执行"的定义——**即使工作流被暂停后恢复，仍算同一次执行，不是两次**。
- `GraphRuntimeState`（`src/graphon/runtime/graph_runtime_state.py:224-231`）：明说 "**Mutable** runtime state shared across graph execution components"，并强调"初始化前就确定、且执行期间不变的值，应该放进 `GraphInitParams` 而不是这里"。

记忆口诀：**`GraphInitParams` 是"出生就定死的身份证"，`GraphRuntimeState` 是"一路在变的账本"，`VariablePool` 是账本里专门记业务数据的那一页。**

---

## 3. 核心状态载体：数据到底存在哪

三个嵌套的状态容器，粒度从粗到细：

```
GraphRuntimeState（总账本，全局唯一、所有组件共享同一实例）
 ├── VariablePool                 数据桥：节点输出/输入的键值存储
 ├── ready_queue                  待执行任务队列（Worker 从这里领活）
 ├── deferred_ready_queue         暂停时暂存未领取任务的"冷藏柜"
 ├── llm_usage / outputs / node_run_steps   token 累计 / 最终输出 / 步数计数
 ├── container_runs / container_frames       容器节点挂起状态、子帧快照
 └── GraphExecution（整图状态机，聚合根）
       └── NodeExecution × N       每个节点一条：execution_id + retry_count
```

### 3.1 GraphRuntimeState —— 总账本

定义在 `src/graphon/runtime/graph_runtime_state.py:223`。构造函数（同文件 `235-277`）初始化了上面列出的所有字段。

**关键约束**：GraphEngine 的所有子系统（Worker、Dispatcher、StateManager、每个节点）**必须共享同一个 `GraphRuntimeState` 实例**。引擎构造时会做一致性校验（`src/graphon/graph_engine/graph_engine.py:196-205` 的 `_validate_graph_state_consistency`），如果发现某个节点用了不同的实例就直接抛错。

它还负责序列化（`dumps` / `from_snapshot`，同文件 `365-437`），这是暂停/恢复能力的基础。

### 3.2 GraphExecution —— 整图状态机（聚合根）

定义在 `src/graphon/graph_engine/domain/graph_execution.py:80`。它管**整张图的生命周期状态**，本质是个状态机：

- 字段：`started` / `completed` / `aborted` / `paused` / `error` / `exceptions_count` / `pause_reasons`
- 迁移方法（`graph_execution.py:100-136`）：`start()`、`complete()`、`abort()`、`pause()`、`fail()`

Dispatcher 每轮循环就是查它的这些标志来决定"继续还是停止"。

### 3.3 NodeExecution —— 单节点执行记录

定义在 `src/graphon/graph_engine/domain/node_execution.py:6`。极简，只有两个字段：

```python
@dataclass
class NodeExecution:
    execution_id: str      # 这次节点执行的唯一 ID
    retry_count: int = 0   # 重试次数
```

由 `GraphExecution.get_or_create_node_execution((frame_id, node_id))` 按 `(帧, 节点)` 为键管理（`graph_execution.py:138-147`）。作用：给节点执行一个身份、记录重试。

---

## 4. 三线程 + 双队列：整体运行骨架

引擎本质是一个**生产者-消费者模型**，跨三个角色协作，通过两条队列解耦：

```
                    ┌──────────── 账本 GraphRuntimeState（全局共享）─────────────┐
                    │  VariablePool     ready_queue     GraphExecution(状态机)     │
                    └──────┬──────────────┬──────────────┬────────────────────────┘
                     数据流 │        任务流 │       状态查询 │
                           ▼               ▼               ▼
   ┌─ Worker×N ─┐  get   ┌─ready_queue─┐         ┌──── Dispatcher（大脑, 单线程）────┐
   │ node.run() │◀───────┤             │         │ 查 graph_execution + state_mgr    │
   │   ↓ yield  │        └─────────────┘         │ 判继续 / 停 / 暂停                 │
   │  event     │──put──▶ event_queue ──get────▶ │ event_handler.dispatch(task)      │
   └─────┬──────┘                                └──────────┬────────────────────────┘
         │ 用 frame_id 查                                    │ 用 frame_id 查
         ▼                                                  ▼
    FrameRegistry ──▶ ExecutionFrame(graph, StateManager, EdgeProcessor, ErrorHandler)
                                              │ _complete_node 时:
                                              ├─ EdgeProcessor 算下游 ──┐
                                              └─ StateManager.enqueue ──┘──▶ 回填 ready_queue（闭环!）
```

三条贯穿全程的数据流：

- **① 任务流**：`StateManager ──enqueue──▶ ready_queue ──get──▶ Worker`
- **② 事件流**：`Worker ──put──▶ event_queue ──get──▶ Dispatcher ──▶ EventHandler`
- **③ 数据流**：`节点 outputs ──store──▶ VariablePool ──read──▶ 下游节点`

推进层（StateManager / EdgeProcessor）是①和②的**转换器**：它消费事件流、产出新的任务流，这就是图能一步步往前滚的闭环。

三个角色的职责边界：

| 角色 | 是谁 | 读什么 | 写什么 | 线程数 |
|---|---|---|---|---|
| **Worker** | 执行工人 | `ready_queue` 领任务 | 只写 `event_queue`（绝不碰图本身） | 多个 |
| **Dispatcher** | 决策大脑 | `event_queue` 取事件 | 改节点/边状态、写 VariablePool、往 `ready_queue` 塞新任务 | 单个 |
| **EventManager** | 传话人 | Dispatcher 让它 collect | 主线程 `emit_events()` yield 出去 | 跨线程 |

两条队列的载荷：

| 队列 | 生产者 | 消费者 | 载荷类型 |
|---|---|---|---|
| `ready_queue` | Dispatcher（经 StateManager） | Worker | `StartTask` / `ResumeTask` |
| `event_queue` | Worker | Dispatcher | `TaskEvent` / `ContainerAwaitTask` |

---

## 5. 装配期：GraphEngine 如何把组件织成一张网

`GraphEngine.__init__`（`src/graphon/graph_engine/graph_engine.py:71-194`）在构造期把所有组件的引用关系织好，这决定了运行时"谁能调谁"。按顺序：

1. **绑定基础**：持有 graph、graph_runtime_state、command_channel；调 `graph_runtime_state.attach_graph(graph)` 让 state 和 graph 双向绑定（`graph_engine.py:82-84`）。
2. **取出状态机**：`self._graph_execution = graph_runtime_state.graph_execution`，并设 `workflow_id`（`graph_engine.py:89-90`）。这个状态机引用会被 Dispatcher 和 EventHandler 同时持有——所以 EventHandler 改状态、Dispatcher 立刻能读到。
3. **建 event_queue**：`queue.Queue[DispatchTask]`（`graph_engine.py:93`）——Worker 和 Dispatcher 唯一的共享桥梁。
4. **建推进三件套 + ROOT 帧**：`GraphStateManager`（`graph_engine.py:97-101`）、`SkipPropagator`、`EdgeProcessor`，打包进 `ExecutionFrame(ROOT_FRAME_ID, ...)`，注册进 `FrameRegistry`（`graph_engine.py:126-135`）。
5. **命令处理器**：`CommandProcessor` 注册 `AbortCommand` / `PauseCommand` / `UpdateVariablesCommand` 三个处理器（`graph_engine.py:139-150`）。
6. **容器处理器**：把 `LoopContainerHandler`、`IterationContainerHandler` 按 node_type 建好（`graph_engine.py:57-60、153-160`）。
7. **WorkerPool**：拿到 `ready_queue`、`event_queue`、`frame_registry`（`graph_engine.py:163-170`）。
8. **EventHandler**：拿到 `graph_execution`、`event_manager`、`frame_registry`、`container_handlers`（`graph_engine.py:174-179`）。
9. **Dispatcher**：把上面所有东西串起来（`graph_engine.py:182-190`）。

**关键交接物**：
- `event_queue` —— Worker 与 Dispatcher 的唯一共享对象。
- `frame_registry` —— 所有组件"查当前该在哪个图上操作"的公共入口。
- `graph_execution` —— 状态机引用，被 Dispatcher 和 EventHandler 共享。

---

## 6. 点火：第一个任务如何入队

`engine.run()`（`src/graphon/graph_engine/graph_engine.py:222-240`）是对外的生成器出口，内部委托 `_run_graph`（`242-263`）：

```python
def _run_graph(self):
    self._event_manager.reset()
    self._initialize_layers()                       # layers.on_graph_start()
    resume = self._graph_execution.started           # 判断首次启动 or 暂停恢复
    if not resume:
        self._graph_execution.start()                # 状态机: started = True
    started_event = GraphRunStartedEvent(...)
    yield started_event                              # ① 产出第一个事件
    self._start_execution(resume=resume)             # ② 启动线程 + root 入队
    yield from self._event_manager.emit_events()     # ③ 流式产出执行期事件（核心）
    yield from self._emit_terminal_events()          # ④ 产出终态事件
```

`_start_execution`（`graph_engine.py:324-364`）首次启动的关键顺序：

```python
self._worker_pool.start()                            # 先起消费者（顺序关键: put 可能阻塞)
root_node = self._graph.root_node
self._state_manager.enqueue_node(root_node.id)       # 把 root 入队
self._dispatcher.start()                             # 起大脑线程
```

`StateManager.enqueue_node`（`src/graphon/graph_engine/graph_state_manager.py:48-63`）的三个原子动作：

```python
def enqueue_node(self, node_id):
    with self._lock:
        self._graph.nodes[node_id].state = NodeState.TAKEN   # ① 节点标 TAKEN
        self._unfinished_nodes.add(node_id)                   # ② 记账: 还有活没干完
        self._graph_runtime_state.enqueue_ready_task(         # ③ 塞进 ready_queue
            StartTask(frame_id=self._frame_id, node_id=node_id),
        )
```

此刻 `_unfinished_nodes = {root}`，ready_queue 里躺着一个 StartTask，两个线程开始转。

---

## 7. 节点执行机制：生成器、事件流、Worker

### 7.1 node.run() 是惰性生成器

`Node.run()`（`src/graphon/nodes/base/node.py:634-667`）是所有节点执行的**统一入口/模板方法**，返回类型是 `Generator[GraphNodeEventBase | ContainerAwaitRequest, None, None]`。

关键认知：**调用 `node.run()` 不会执行任何业务逻辑**，只是创建一个生成器对象。函数体要等到被**迭代**时才逐段执行。所以 Worker 里 `node_events = node.run()`（`worker.py:213`）这一行执行完时，节点一行代码都还没跑。

`run()` 固定做三件事：

```python
def run(self):
    start_event = NodeRunStartedEvent(...)      # ① 每个节点都必发 Started 事件
    self.populate_start_event(start_event)
    yield start_event
    try:
        yield from self._run_events()            # ② 执行真正逻辑（委托给 _run）
    except Exception as e:
        yield self._build_run_failed_event(e)    # ③ 兜底: 异常转成失败事件, 不炸穿 Worker
```

### 7.2 _run() 的两种形态

`_run()` 是抽象方法（`node.py:620-632`），子类实现，返回 `NodeRunResult | Generator`。`_run_events`（`node.py:669-682`）统一处理两种形态：

```python
def _run_events(self):
    result = self._run()
    if isinstance(result, NodeRunResult):        # 形态①: 一次性结果（如 code 节点）
        yield self._convert_node_run_result_to_graph_node_event(result)   # 包成 1 个事件
        return
    for event in result:                          # 形态②: 流式生成器（如 llm 节点）
        yield self._normalize_run_event(event)    # 逐个转发（多个事件）
```

- **CodeNode._run**（`src/graphon/nodes/code/code_node.py:143-195`）：签名返回 `NodeRunResult`，函数体无 `yield`，同步跑完代码 → `return NodeRunResult(...)`。因为代码执行没有可流式展示的中间态。
- **LLMNode._run**（`src/graphon/nodes/llm/node.py:196`）：签名返回 `Generator`，函数体布满 `yield`，边收模型 token 边 yield `StreamChunkEvent`。因为模型天然逐 token 生成。

**结论**：一个节点产出的是**一串事件**，不是一个。简单节点至少 2 个（`Started` + `Succeeded`），LLM 节点可能几十上百个（`Started` + N×`StreamChunk` + `Succeeded`）。

### 7.3 为什么用多事件设计

| 目的 | 说明 |
|---|---|
| 流式输出 | LLM 逐 token 吐，chunk 一产出就投递 → 打字机效果 |
| 生命周期可观测 | Started/Succeeded/Failed/Retry 让每个阶段对外可见 |
| 统一通信协议 | Worker 只管转发事件，不需理解节点在干什么 |
| 支持挂起恢复 | 节点可中途 yield `ContainerAwaitRequest` 停在半路 |

### 7.4 Worker 如何驱动生成器

Worker 主循环 `run()`（`src/graphon/graph_engine/worker.py:120-167`）：抢任务 → 执行 → 收尾 → 领下一个。用 `task_claim_lock` 保证多 Worker 原子领取（`worker.py:128-141`）。

真正驱动生成器的是 `_consume_node_events`（`worker.py:254-296`）：

```python
def _consume_node_events(self, *, invocation_id, node, node_events):
    result_event = None
    for event in node_events:                              # ← 这里第一次迭代才真正执行节点
        if isinstance(event, ContainerAwaitRequest):        # 分支A: 容器挂起信号
            # 存 container_run 凭据, 投 ContainerAwaitTask
            return None, True                               # 提前返回, suspended=True, 冻结节点
        if isinstance(event, NodeRunStartedEvent) and event.id == node.execution_id:
            self._current_node_started_at = event.start_at  # 记开始时间
        self._event_queue.put(TaskEvent(frame_id=..., event=event))   # 通用: 投递事件
        if is_node_result_event(event):
            result_event = event                            # 记录最终结果事件
    return result_event, False                              # 正常跑完, suspended=False
```

**重点**：`for` 循环是"生产者（节点）与消费者（Worker）交替推进"的驱动引擎——拉一次、节点跑一段到下一个 `yield`、投一个事件、再拉。这既实现了流式，也让"中途挂起"成为可能（遇到 `ContainerAwaitRequest` 直接 `return` 跳出，节点冻结）。

外层 `_run_node_events`（`worker.py:226-252`）负责 layer 钩子和异常处理：

```python
with self._execution_context:                    # 线程上下文桥接（默认 nullcontext）
    if invocation_id is None:
        self._invoke_node_run_start_hooks(node)  # 首次执行才发 on_node_run_start
    try:
        result_event, suspended = self._consume_node_events(...)
    except Exception as exc:
        error = exc
        raise
    else:
        return suspended
    finally:
        if not suspended:                         # 挂起时不发 end 钩子（节点还没真结束）
            self._invoke_node_run_end_hooks(node, error, result_event)
```

`invocation_id is None` 区分"首次执行"与"容器恢复"；`if not suspended` 保证一个节点的 start/end 钩子在完整生命周期里各触发恰好一次，即使经历任意多次挂起/恢复。

---

## 8. 事件消费与图推进：Dispatcher 与 EventHandler

### 8.1 Dispatcher 主循环

`Dispatcher._dispatcher_loop`（`src/graphon/graph_engine/orchestration/dispatcher.py:97-124`）单线程运行：

```python
def _run_until_exit(self):
    self._process_commands()
    while not self._stop_event.is_set():
        if (self._graph_execution.aborted
                or self._graph_execution.error is not None
                or self._state_manager.is_execution_complete()):   # 查状态机 + 记账
            return False
        if self._graph_execution.paused:
            self._state_manager.defer_ready_tasks(self._worker_pool.drain())
            return True
        self._worker_pool.check_and_scale()          # 动态扩缩容
        self._dispatch_next_event()                  # 取事件 → 分派
    return False
```

每轮同时查两个东西：`graph_execution`（状态机，管全局状态）和 `state_manager.is_execution_complete()`（记账，`_unfinished_nodes` 是否为空）。

### 8.2 EventHandler 按类型分派

`EventHandler`（`src/graphon/graph_engine/event_management/event_handlers.py`）用 `@singledispatchmethod` 按事件类型注册处理器（`event_handlers.py:160-352`）。事件分三类待遇：

**类别 A：纯观察事件——只收集，不改图**（`event_handlers.py:171-192`）
```python
@_dispatch.register
def _(self, event: (NodeRunStreamChunkEvent | NodeRunReasoningChunkEvent
                    | NodeRunLoopNextEvent | ...), *, frame):
    self._collect(frame=frame, event=event)     # 只 collect 转发, 不碰图状态
```
LLM 的几十个 chunk 就是这样被"消费"的——快速转发给观察者。

**类别 B：结果事件——推进图**（最重要，`event_handlers.py:318-352`）
```python
def _complete_node(self, *, frame, event, follow_branch):
    frame.graph_runtime_state.add_llm_usage(...)               # 累计 token
    self._store_node_outputs(...)                               # ⭐ 数据流: 写 VariablePool
    if follow_branch:
        ready_nodes, edge_events = frame.edge_processor.handle_branch_completion(...)
    else:
        ready_nodes, edge_events = frame.edge_processor.process_node_success(...)  # ⭐ 算下游
    for node_id in ready_nodes:
        frame.state_manager.enqueue_node(node_id)               # ⭐ 新任务流: 下游入队(闭环!)
    if node.execution_type == NodeExecutionType.RESPONSE:
        frame.graph_runtime_state.merge_response_outputs(...)   # 响应节点合并进 outputs
    frame.state_manager.finish_execution(event.node_id)         # 记账: 本节点完工
    self._collect(frame=frame, event=event)
```

**类别 C：特殊控制事件**：`NodeRunStartedEvent`（记步数/retry 计数，`event_handlers.py:194-212`）、`NodeRunFailedEvent`（交 ErrorHandler，`248-276`）、`NodeRunRetryEvent`（重新入队，`295-316`）、`NodeRunPauseRequestedEvent`（触发暂停，`236-246`）。

### 8.3 关于 chunk 聚合的澄清

**EventHandler 不聚合 chunk**。LLM 的完整结果由**节点内部**累加好，随最后的 Succeeded 事件一次性交出：

- 累加点：`src/graphon/nodes/llm/node.py:1014` 的 `state.full_text_buffer.write(text_part)`——每段文本一边写进 `StringIO` 缓冲区、一边 yield 成流式 chunk（同源分流）。
- 取出点：`node.py:911` 的 `full_text = state.full_text_buffer.getvalue()` → 装进 `ModelInvokeCompletedEvent.text` → 组装成 `NodeRunResult.outputs`。
- 落地点：EventHandler 的 `_store_node_outputs` 把它写进 VariablePool 供下游读。

**过程事件（chunk）多个 + 结果事件（Succeeded）一个，JSON/完整文本搭在结果事件里出去。**

---

## 9. 上下游流转判定：边三态与就绪算法

这是引擎调度的算法核心。**流转判断的载体不是节点，是边。**

### 9.1 边的三态

每条 `Edge`（`src/graphon/graph/edge.py:15`）有一个 `state` 字段，三种取值：

| 边状态 | 含义 | 何时变成它 |
|---|---|---|
| `UNKNOWN` | 未定——上游没跑完，不知道这条边走不走 | 初始状态 |
| `TAKEN` | 走了——上游成功且选了这条路 | 上游成功后 EdgeProcessor 标记 |
| `SKIPPED` | 不走——上游被跳过，或分支未选中它 | SkipPropagator 传播 |

**心智模型**：整张图的执行过程 = 把所有边从 `UNKNOWN` 逐步敲定成 `TAKEN` 或 `SKIPPED`。节点能不能跑，完全由它**入边的状态组合**决定。

### 9.2 正向流转：上游成功 → 出边 TAKEN → 检查下游

`EdgeProcessor.process_node_success`（`src/graphon/graph_engine/graph_traversal/edge_processor.py:43-79`），对每条出边调 `_process_taken_edge`（`93-117`）：

```python
def _process_taken_edge(self, edge):
    self._state_manager.mark_edge_taken(edge.id)          # ① 出边 → TAKEN
    if self._state_manager.is_node_ready(edge.head):      # ② 问下游: 你够格跑了吗?
        ready_nodes.append(edge.head)                     # ③ 够格 → 待入队
    return ready_nodes, [taken_event]
```

### 9.3 就绪判定：is_node_ready

`GraphStateManager.is_node_ready`（`src/graphon/graph_engine/graph_state_manager.py:75-101`），三条规则：

```python
def is_node_ready(self, node_id):
    incoming_edges = self._graph.get_incoming_edges(node_id)
    if not incoming_edges:                                     # 规则0: 无入边(root) → 就绪
        return True
    if any(edge.state == UNKNOWN for edge in incoming_edges):  # 规则1: 有 UNKNOWN → 不能跑
        return False
    return any(edge.state == TAKEN for edge in incoming_edges) # 规则2: 至少一条 TAKEN → 能跑
```

| 入边状态组合 | 能跑吗 | 原因 |
|---|---|---|
| 无入边 | 是 | root 节点，起点 |
| 有任何 UNKNOWN | 否（等） | 还有上游没表态 |
| 全部已定 + 至少一条 TAKEN | 是 | 有活路通到我 |
| 全部已定 + 全是 SKIPPED | 否（自己也被跳过） | 所有上游都没走到我 |

### 9.4 分支与跳过传播

`BRANCH` 类型节点（if-else / question-classifier）成功后走 `handle_branch_completion`（`edge_processor.py:119-150`）：选中 handle 的边标 TAKEN，其余边交给 `SkipPropagator.skip_branch_paths`。

`SkipPropagator`（`src/graphon/graph_engine/graph_traversal/skip_propagator.py`）递归传播跳过（`_propagate_skip_to_node`, `76-94`）：节点被跳过 → 所有出边标 SKIPPED → 对每条出边的下游递归 `propagate_skip_from_edge`。这套递归保证图不会死锁——任何边最终都会被敲定。

---

## 10. 多入边节点如何确定下来

多入边节点（join 点）必须**等所有上游都表态**。它在**最后一条 UNKNOWN 入边被敲定**的那一刻确定命运。

关键机制在 `SkipPropagator.propagate_skip_from_edge`（`skip_propagator.py:36-70`）：

```python
def propagate_skip_from_edge(self, edge_id):
    downstream = self._graph.edges[edge_id].head     # J（多入边节点）
    incoming = self._graph.get_incoming_edges(downstream)
    states = self._state_manager.analyze_edge_states(incoming)

    if states["has_unknown"]:                        # 还有 UNKNOWN → 停, 等它
        return []
    if states["has_taken"]:                          # 有 TAKEN → J 可以跑!
        self._state_manager.enqueue_node(downstream) # ⭐ 在这里把 J 入队
        return []
    if states["all_skipped"]:                        # 全 SKIPPED → J 也跳过
        return self._propagate_skip_to_node(downstream)
```

**J 可能从两个方向被触发确定**：

1. **正向（有边 TAKEN）**：`is_node_ready`（`edge_processor.py:114`）——某上游成功标 TAKEN 时，若 J 其他入边恰好都已定，直接就绪。
2. **反向（有边 SKIPPED）**：`propagate_skip_from_edge`（`skip_propagator.py:62-63`）——跳过传播到最后一条边，发现还有 TAKEN，把 J 入队。

**无论哪条边最后被敲定，敲定它的动作都会重新检查 J**。这保证 J 一定在"所有入边都非 UNKNOWN"的那一刻、且仅一次被正确处理（要么 enqueue，要么 skip）。

举例（分支两条边 A、B 汇聚到 J，选了 A）：
- T1：分支成功，`edge_A` → TAKEN，检查 J：`edge_B` 还是 UNKNOWN → 不能跑，按兵不动。
- T2：跳过传播处理 `edge_B` → SKIPPED，检查 J：入边 = {TAKEN, SKIPPED}，无 UNKNOWN 且有 TAKEN → **J 就绪入队**。
- 若 A、B 都没选到 J，则 J 入边最终全 SKIPPED → **J 自己也被跳过**，继续向下传播。

---

## 11. 一次完整执行的时间轴（start → llm → answer）

以最小图为例把所有机制串起来。图结构：

```
start(id=s) → llm(id=L, 输出 {text}) → answer(id=A, 模板 "{{#llm.text#}}")
边: edge_0(s→L), edge_1(L→A)
```

| 时刻 | 谁 | 动作 |
|---|---|---|
| t0 | 引擎 | `enqueue_node("s")`：s 标 TAKEN，`_unfinished={s}`，StartTask 入 ready_queue |
| t1 | Worker | 领 StartTask(s)，`node.run()` 产出 `Started(s)` → `Succeeded(s, outputs={query})` |
| t2 | Worker | 两个事件包成 TaskEvent 投 event_queue |
| t3 | Dispatcher | 处理 `Started(s)`：记步数 + collect |
| t4 | Dispatcher | 处理 `Succeeded(s)` → `_complete_node`：写 `VariablePool[(s,query)]`；`edge_0` → TAKEN；`is_node_ready(L)`=True → `enqueue_node("L")`；`finish_execution("s")`。此刻 `_unfinished={L}` |
| t5 | Worker | 领 StartTask(L)，读 `VariablePool[(s,query)]` 拼 prompt，调 `SlimLLM.invoke_llm` |
| t6 | Worker | 逐 token yield：`Started(L)` → `StreamChunk("G")` → ... → `Succeeded(L, outputs={text:"Graphon"})`，逐个投 event_queue |
| t6' | Dispatcher | chunk 事件走"只 collect"分支，不推进图 |
| t7 | Dispatcher | 处理 `Succeeded(L)`：写 `VariablePool[(L,text)]="Graphon"`；`edge_1` → TAKEN；`enqueue_node("A")`；`finish_execution("L")`。此刻 `_unfinished={A}` |
| t8 | Worker | 领 StartTask(A)，读 `VariablePool[(L,text)]` 填模板，产出 `Succeeded(A, outputs={answer:"Graphon"})` |
| t9 | Dispatcher | `_complete_node`：写 outputs；A 无出边 → ready_nodes=[]；A 是 RESPONSE → `merge_response_outputs`；`finish_execution("A")`。此刻 `_unfinished={}` |
| t10 | Dispatcher | `is_execution_complete()`=True → 退出循环 → `graph_execution.complete()` → `event_manager.mark_complete()` |
| t11 | 主线程 | `emit_events` 见 mark_complete，yield 完剩余事件 → `_emit_terminal_events` 发 `GraphRunSucceededEvent(outputs={answer:"Graphon"})` |

**数据流接力**：`(s,query)` → L 读它拼 prompt → `(L,text)` → A 读它填模板 → `outputs["answer"]`。VariablePool 是节点间唯一的数据桥。

---

## 12. 扩展节点之一：插件类节点（tool / llm）

插件类节点（`tool`、`llm`）需要调用**外部插件/运行时**来干活。它们在引擎里的运行流程与普通节点**完全相同**，唯一区别在内部实现方式：**通过"运行时协议注入"把活外包给适配器。**

### 12.1 节点只定义"要什么运行时"，不实现运行时

`ToolNode.__init__`（`src/graphon/nodes/tool/tool_node.py:77-97`）构造时要求注入 `runtime`：

```python
def __init__(self, node_id, data, *, graph_init_params, graph_runtime_state,
             tool_file_manager: ToolFileManagerProtocol,
             runtime: ToolNodeRuntimeProtocol):     # ← 注入"工具运行时"协议
    ...
    self._runtime = runtime
```

`ToolNodeRuntimeProtocol`（`src/graphon/nodes/runtime.py:19-61`）是个**协议接口**，只声明能力不实现：`get_runtime`、`get_runtime_parameters`、`invoke`、`get_usage`、`build_file_reference`。真正的实现由使用方（`core.workflow` 层）注入，不在 graphon 里。

LLMNode 是同一个模式——构造时注入 `model_instance`（`SlimLLM`，实现 `LLMProtocol`）。**tool 注入 runtime，llm 注入 model_instance，本质一样：节点持有一个"外包接口"的引用。**

### 12.2 _run 把实质工作委托给适配器

`ToolNode._run`（`tool_node.py:110-189`）结构和流式节点一样，但每步实质工作委托 `self._runtime`：

```python
def _run(self):
    tool_runtime = self._get_tool_runtime(...)                   # 委托: 拿运行时句柄
    tool_parameters = self._runtime.get_runtime_parameters(...)  # 委托: 拿参数定义
    parameters = self._generate_parameters(...)                  # 节点做: 从 VariablePool 填参数
    message_stream = self._runtime.invoke(                       # 委托: 真正执行工具(调插件)
        tool_runtime=tool_runtime, tool_parameters=parameters, ...)
    yield from self._transform_message(messages=message_stream, ...)  # 节点做: 消息 → 图事件
```

**分工**：节点负责"从 VariablePool 读输入、填参数、把适配器返回的原始消息翻译成图事件"；runtime 负责"连接插件、执行、返回流式消息"。`invoke` 返回 `Generator[ToolRuntimeMessage]`（`runtime.py:44-51`），所以工具节点也是流式的。

### 12.3 协议注入的层次

```
graphon 引擎          ← 只认 node.run() 吐事件, 不关心节点类型
    │
节点(ToolNode/LLMNode) ← 定义"我需要什么运行时协议", 负责填参数 + 翻译事件
    │ 持有
运行时协议接口         ← ToolNodeRuntimeProtocol / LLMProtocol (graphon 定义接口)
    │ 由外部实现并注入
适配器实现            ← SlimLLM / 工具运行时适配器 (连接 Slim daemon / 插件守护进程)
    │ 进程通信 (subprocess + stdin/stdout NDJSON)
外部插件              ← dify-plugin-daemon 里的工具/模型插件 (graphon 之外)
```

**三层解耦的意义**：graphon 引擎不依赖任何插件 SDK；节点只依赖协议接口；换后端（本地 slim / 远程 daemon / 别的实现）只需换注入的适配器，节点和引擎都不动。

### 12.4 在流转上无特殊

- 输入：`_generate_parameters` 从 `VariablePool` 读上游变量填参数（`tool_node.py:151-155`）。
- 输出：`_transform_message` 把工具结果转成 Succeeded 事件，outputs 被 `_complete_node` 存进 VariablePool。
- 就绪判定/边流转：完全走第 9 节的 `is_node_ready` + EdgeProcessor，没有特殊路径。

---

## 13. 扩展节点之二：容器类节点（loop / iteration）

容器节点内部还嵌套一张**子图**要反复跑。核心思想：**容器节点不自己跑子图，而是"挂起自己 → 请求引擎帮它跑子图 → 恢复自己"，完全复用主图机制。**

### 13.1 协作式挂起

```
容器节点执行 → yield ContainerAwaitRequest("我要跑子图, 请帮我") → 挂起, 交回控制权
引擎收到请求 → ContainerHandler 造子帧, 在子帧里用同一套引擎机制跑子图
子图跑完   → ResumeTask 唤醒容器节点, 把子图结果交给它
容器节点   → 决定: 再跑一轮(再 yield 请求)? 还是结束(yield 最终 Succeeded)?
```

`ContainerAwaitRequest`（`src/graphon/nodes/container_effects.py:75`）分 `LoopFrameRequest` 和 `IterationFrameRequest`，携带跑子图所需信息（root_node_id、循环变量、当前 index 等）。

### 13.2 四个关键角色

| 角色 | 作用 |
|---|---|
| `ContainerAwaitRequest` | 容器节点发出的"帮我跑子图"请求 |
| `ContainerHandler`（loop/iteration 各一个） | 引擎侧"子图管家"，造子帧、管循环逻辑 |
| `ExecutionFrame` | 子图的独立执行环境 |
| `ContainerRunState` | 挂起时存的"待恢复凭据" |

`ContainerHandler` 协议（`src/graphon/graph_engine/container_handlers.py:16-54`）：`start_await`（启动子图）、`prepare_frame_event`（给子图事件打容器标记）、`should_collect`、`complete_frame`、`restore_frame`。

### 13.3 运行流程（以 loop 为例）

**阶段1 挂起（Worker 侧）**：容器节点 yield `ContainerAwaitRequest`，Worker（`worker.py:263-288`）生成 `invocation_id`、`put_container_run` 存待恢复凭据、投 `ContainerAwaitTask`、`return None, True` 冻结节点。

**阶段2 启动子图（Dispatcher → Handler）**：`EventHandler.start_container` → `LoopContainerHandler.start_await`（`src/graphon/graph_engine/loop_container_handler.py:62-104`）：

```python
def start_await(self, *, invocation_id, request):
    if self._loop_break_conditions_reached(...):     # ① 该结束了吗?
        self._enqueue_container_result(...)          # 达到 break → 产出最终结果
        return
    self._start_loop_frame(...)                       # ② 否则启动一轮子图
```

`_start_loop_frame` 调 `FrameRegistry.materialize_child_frame`（`src/graphon/graph_engine/frames.py:53-102`）造子帧：

```python
graph = Graph.init(graph_config, rebound_factory, root_node_id=子图起点)  # 子图也是一张 Graph
state_manager = GraphStateManager(graph, ..., frame_id=子帧id)            # 子图独立记账
edge_processor = EdgeProcessor(graph, state_manager, skip_propagator)     # 子图独立边处理
frame = ExecutionFrame(子帧id, graph, runtime_state, state_manager, edge_processor, error_handler)
frame_registry.register(frame)
```

**子帧的隔离与共享**（`frames.py:112-121`）：
- 独立：自己的 graph、StateManager、EdgeProcessor（子图流转与主图互不干扰）
- 共享：同一个 `ready_queue`、`graph_execution`（子图节点进主队列，被同一批 Worker 执行）

**阶段3 子图正常跑**：与主图**完全一样**，唯一区别是 TaskEvent 带子帧 frame_id，Dispatcher 处理时 `frame_registry.get(子帧id)` 拿子帧组件。事件流经 `prepare_frame_event`（`loop_container_handler.py:106-130`）打上 `loop_id`、`loop_index` 标记。

**阶段4 唤醒决定下一步**：子图跑完，Handler 产出 `ResumeTask(invocation_id, result)` 塞回 ready_queue。Worker（`worker.py:185-199`）领到后用 `get_container_run` 找回挂起节点，调 `node.resume_container(result=子图结果)` 唤醒。节点决定：再 yield 请求（下一轮，复用 invocation_id）或 yield 最终 Succeeded（结束，`pop_container_run` 清理）。

**阶段5 真正完成**：最后的 Succeeded 走普通 `_complete_node`——存 outputs、标出边、算下游。从主图视角，容器节点和普通节点无区别，只是内部偷偷跑了 N 轮子图。

### 13.4 递归复用的精髓

引擎核心（Worker/Dispatcher/队列）对"容器"**零感知**，只处理 `frame_id + 任务 + 事件`。容器逻辑全封装在 `ContainerHandler` + 节点的 `resume_container` 里。所以：子图可任意嵌套（子图里还能有 loop，每层再造一个 frame）；子图自动获得主图所有能力（并行、分支、错误处理、就绪判定、暂停恢复）。

---

## 14. 事件的流式输出与整理（filters 层）

引擎 `run()` 吐出的原始事件是**并行、乱序**的（多节点同时跑，chunk 交错）。对终端用户，响应节点需要**按模板顺序、串行**呈现。这个整理由**引擎之外**的 `ResponseStreamFilter` 完成。

- 挂载方式：`filter_graph_events(engine.run(), context=..., filters=[ResponseStreamFilter()])`（见 `examples/slim_llm/dsl.py:40-44`）。
- 本质：`on_event(event) -> Iterable[event]` 的 1→N 变换器链（`src/graphon/graph_engine/filters/chain.py:10-34`），完全运行在调用方线程，不碰引擎的线程/队列。
- 核心结构（`src/graphon/graph_engine/filters/response_stream.py`）：`ResponseSession`（响应节点的流式游标）、`Path`（阻塞边，决定何时轮到某响应节点开始 stream）、`StreamBuffers`（按 selector 分桶缓冲 chunk）、`_try_flush`（按模板段顺序吐出）。

**两层管线**：引擎内产出并行乱序的原始事件流 → 引擎外 filter 整理成按响应节点/模板顺序的有序流。

---

## 15. 暂停与恢复

- 外部命令通过 `CommandChannel` 投递（abort / pause / update_variables），Dispatcher 在事件间隙 `process_commands()` 处理（`dispatcher.py:144-146`）。
- 暂停时：把未领取任务转入 `deferred_ready_queue`，`snapshot_frames()` 快照子帧（`graph_engine.py:266-278`、`event_handlers.py:132-152`）。
- 恢复时：`run()` 检测到 `graph_execution.started` 为真，走 resume 分支（`graph_engine.py:324-364`）——恢复容器帧、重新入队 deferred 任务。
- 整个执行的序列化靠 `GraphRuntimeState.dumps()` / `from_snapshot()`（`graph_runtime_state.py:365-437`）和 `GraphExecution.dumps()` / `loads()`（`graph_execution.py:149-228`）。

注意：暂停后恢复仍算**同一次执行**（见第 2 节 `GraphInitParams` 的定义）。

---

## 16. 关键设计思想总结

1. **四支柱**：静态配置（GraphInitParams）+ 可变账本（GraphRuntimeState）+ 帧隔离（ExecutionFrame）+ 单线程推进（Dispatcher）。
2. **执行与决策分离**：Worker 多线程只执行、不决策；Dispatcher 单线程做所有图状态写操作，天然无锁。
3. **边三态驱动流转**：不看节点看边（UNKNOWN/TAKEN/SKIPPED），节点就绪由入边组合推导；每次一条边被敲定就重查其下游。
4. **事件流解耦**：层层都用"吐事件"通信，Worker/Dispatcher 对节点类型零感知；一个节点产出多个事件以支持流式、可观测、挂起。
5. **协议注入（插件类节点）**：引擎不依赖任何插件 SDK，节点只依赖运行时协议接口，换后端只换适配器。
6. **递归复用（容器类节点）**：子图 = 又一个 ExecutionFrame = 又一遍完整引擎流程，可任意嵌套。
7. **两层管线**：引擎内产出并行乱序事件，引擎外 filter 整理成有序流。

---

## 17. 组件能力速查表

| 类 | 层 | 一句话能力 | 主要位置 |
|---|---|---|---|
| `GraphEngine` | 编排 | 总装配 + `run()` 生成器出口 | `graph_engine/graph_engine.py:64` |
| `Dispatcher` | 编排 | 单线程大脑：消费事件、推进、判停 | `graph_engine/orchestration/dispatcher.py:31` |
| `WorkerPool` | 编排 | 管理 Worker、按队列积压动态扩缩容 | `graph_engine/worker_management/worker_pool.py:27` |
| `Worker` | 编排 | 领任务、执行节点、吐事件，不做决策 | `graph_engine/worker.py:51` |
| `GraphInitParams` | 状态 | 存不变配置（身份证，只读） | `entities/graph_init_params.py:9` |
| `GraphRuntimeState` | 状态 | 存会变的一切、全局共享（账本） | `runtime/graph_runtime_state.py:223` |
| `GraphExecution` | 状态 | 整图生命周期状态机 | `graph_engine/domain/graph_execution.py:80` |
| `NodeExecution` | 状态 | 单节点 execution_id + retry_count | `graph_engine/domain/node_execution.py:6` |
| `VariablePool` | 状态 | 节点间传数据的键值存储 | `runtime/variable_pool.py` |
| `GraphStateManager` | 推进 | 节点入队 + 就绪判定 + 完成判定 | `graph_engine/graph_state_manager.py:23` |
| `EdgeProcessor` | 推进 | 节点成功后算下游该跑谁 | `graph_engine/graph_traversal/edge_processor.py:18` |
| `SkipPropagator` | 推进 | 未选中分支路径标 SKIPPED 并递归传播 | `graph_engine/graph_traversal/skip_propagator.py:14` |
| `ExecutionFrame` | 帧 | 一套图执行环境的打包 | `graph_engine/frames.py:29` |
| `FrameRegistry` | 帧 | frame_id → frame 路由表，支撑嵌套子图 | `graph_engine/frames.py:40` |
| `ContainerHandler` | 容器 | loop/iteration 子图管家 | `graph_engine/container_handlers.py:16` |
| `EventManager` | 事件 | 事件缓存 + 流式 yield + 通知 layers | `graph_engine/event_management/event_manager.py:75` |
| `EventHandler` | 事件 | 按事件类型分派消费逻辑 | `graph_engine/event_management/event_handlers.py:58` |
| `ErrorHandler` | 事件 | 节点失败时决定 retry/fail-branch/整图失败 | `graph_engine/error_handler.py` |
| `Node` | 节点 | 执行模板方法（run→_run），统一事件契约 | `nodes/base/node.py:634` |
| `ResponseStreamFilter` | 输出 | 引擎外把乱序事件整理成有序响应流 | `graph_engine/filters/response_stream.py:207` |

---

# 附录：深度细节

以下附录把正文中一笔带过的机制展开到源码级，供深入排查/二次开发时参考。

---

## 附录 A：LLM 节点内部完整执行链路

正文第 7 节说 LLMNode 是流式节点，这里把它从 `_run` 到真正拿到模型流、再 yield chunk 的**完整调用链**摊开。

### A.1 调用栈全景

```
LLMNode._run                              nodes/llm/node.py:196
  ├─ _prepare_run_prompt                   :241   准备 prompt（读变量、拼消息、模型实例）
  └─ _yield_run_completion                 :354   驱动模型调用 + 组装结果
       └─ _invoke_llm_for_run              :447   决定走直连还是轮询
            └─ LLMNode.invoke_llm          :735   决定结构化 or 普通
                 └─ model_instance.invoke_llm(_with_structured_output)   → SlimLLM
                 └─ handle_invoke_result   :784   区分阻塞结果 or 流式生成器
                      └─ _yield_streaming_invoke_result  :848
                           └─ _yield_streaming_events     :939
                                └─ _handle_stream_result  :957   每个 chunk 分流
                                     └─ _yield_stream_text_events :981
                                          └─ _build_stream_text_events :1002  ⭐ 累加 + yield
```

### A.2 三层分叉（决定"走哪条路"）

一次 LLM 调用要过三层布尔开关，每层由明确条件决定，不靠猜：

**分叉1：结构化 or 普通**（`node.py:749`）
```python
if structured_output_enabled:
    invoke_result = model_instance.invoke_llm_with_structured_output(json_schema=..., stream=True)
else:
    invoke_result = model_instance.invoke_llm(stream=True)
```

**分叉2：轮询 or 直连**（`node.py:454-473`）
```python
polling_model = self._polling_model_instance()      # 模型是否实现 LLMPollingCapableProtocol
if polling_model is None:
    return LLMNode.invoke_llm(...)                    # 直连（SlimLLM 走这条）
return self._invoke_llm_with_polling(...)             # 轮询（异步长任务模型）
```

**分叉3：流式 or 阻塞**（在 SlimLLM 层，`dsl/slim/llm.py:367-390`）
```python
if stream:
    return generator                                  # 流式：返回生成器
return _collect_llm_result(...)                       # 阻塞：收集成一个完整结果
```

`SlimLLM._invoke_llm_internal`（`dsl/slim/llm.py:338`）用 `stream × expect_structured_output` 两个布尔的四种组合，通过 `@overload` 声明四种精确返回类型。所以拿到的 chunk 类型在**发请求时就注定了**。

### A.3 文本累加：同源分流

正文提到累加在 `full_text_buffer`，这里给完整机制（`node.py:1002-1022`）：

```python
def _build_stream_text_events(self, *, text_part, state, node_id):
    if text_part and not state.has_content:
        state.first_token_time = time.perf_counter()      # 记录首 token 时间(用于 usage)
        state.has_content = True
    state.full_text_buffer.write(text_part)               # ① 累加进 StringIO 缓冲区
    if state.text_filter is None:                          # tagged 模式: 直接流原始 token
        yield StreamChunkEvent(selector=[node_id, "text"], chunk=text_part, is_final=False)  # ② 流式
        return
    for piece in state.text_filter.feed(text_part):        # separated 模式: 切 <think>
        yield from self._yield_filter_piece(piece=piece, state=state, node_id=node_id)
```

**关键**：同一个 `text_part`，一份 `write` 进 buffer（未来的完整结果），一份 `yield` 成 chunk 事件（当下的流式展示）。两者并行、互不干扰——这就是"累加 vs 展示同源分流"。

流结束后取出（`node.py:911-929`）：
```python
full_text = state.full_text_buffer.getvalue()             # 取出完整文本
clean_text, reasoning_content = extract_stream_reasoning(full_text=full_text, ...)  # 分离推理
yield ModelInvokeCompletedEvent(text=..., usage=..., reasoning_content=..., structured_output=...)
```

最终装进 `NodeRunResult.outputs`，随 `StreamCompletedEvent` 交出（`node.py:432-443`）。

---

## 附录 B：推理内容（`<think>`）的分流处理

推理模型（如 DeepSeek-R1）把"思考过程"和"最终答案"混在同一条流里，用 `<think>...</think>` 包裹。`reasoning_format` 有两种模式：

- **`tagged`**：不拆，标签原样留在 text（`state.text_filter is None`）
- **`separated`**：拆开，思考走 `reasoning_content` selector，答案走 `text` selector

### B.1 流式切分：ThinkStreamFilter

`ThinkStreamFilter`（`nodes/llm/reasoning.py:48-169`）是**跨 chunk 边界安全**的增量剥离器。难点：标签可能被切成 `"<thi" + "nk>"` 分散在两个 chunk。

核心方法 `feed`（`reasoning.py:70-104`）用一个 `_hold` 缓冲区暂存"可能正在形成标签的结尾几个字符"：

```python
def feed(self, text_part):
    work = self._hold + text_part                     # 拼上次扣留的
    self._hold = ""
    while work:
        if not self._inside_think:                     # 当前在正文
            match = _THINK_OPEN_RE.search(work)         # 找 <think> 开标签
            if match: ... 进入 think 区
            keep = self._open_suffix_len(work)          # 结尾可能是半个标签? 扣下来
            if keep: self._hold = work[-keep:]          # 扣留, 等下个 chunk
        else:                                           # 当前在 think 区内
            match = _THINK_CLOSE_RE.search(work)        # 找 </think>
            ...
```

`_open_suffix_len`（`reasoning.py:142-160`）判断结尾是否可能是半个 `<think` 标签的前缀，是就扣留不吐，避免把半个标签当正文流出去。

### B.2 收尾冲刷：finalize

流结束后（`node.py:889-909`）必须清空 filter 里扣着的残余：
```python
if state.text_filter is not None:
    final_pieces = state.text_filter.finalize()        # 冲出 _hold 残余
    for index, piece in enumerate(final_pieces):
        is_final_reasoning = (piece.kind == "reasoning" and index == len - 1)
        yield from self._yield_filter_piece(piece=piece, ..., is_final=is_final_reasoning)
    if state.reasoning_started and not has_final_reasoning:
        yield StreamReasoningEvent(..., chunk="", is_final=True)   # 补发"推理结束"标记
```

`finalize`（`reasoning.py:106-114`）的关键判断：若流结束时还 `_inside_think`（未闭合标签，模型被截断），残余算 reasoning 而非 text。

### B.3 双轨制：流式 vs 最终

| | ThinkStreamFilter（流式） | extract_stream_reasoning（最终）|
|---|---|---|
| 对象 | 逐 chunk，边到边切 | 累加完的完整 full_text |
| 目的 | 流式展示分流 | 给 outputs 干净的 text + reasoning |
| 位置 | `node.py:889-909` | `node.py:911-915` |

流式路处理增量断裂，数据路对完整文本重新精确切一遍，各自保证正确。

---

## 附录 C：结构化输出（JSON）的完整机制

### C.1 类型继承关系

```python
# model_runtime/entities/llm_entities.py:284
class LLMResultChunkWithStructuredOutput(LLMResultChunk, LLMStructuredOutput):
    ...   # 多继承: 既是文本 chunk, 又带 structured_output 字段
```

这就是为什么处理它时用**两个平行的 `if`**（`node.py:965-978`）而非 `if/elif`——它需要"捞 JSON"和"处理文本"两步都走：

```python
if isinstance(result, LLMResultChunkWithStructuredOutput):   # 捞 JSON
    if result.structured_output is not None:
        state.structured_output = dict(result.structured_output)
    yield result
if isinstance(result, LLMResultChunk):                        # 处理文本（父类, 也成立）
    yield from LLMNode._yield_stream_text_events(...)
    LLMNode._update_streaming_metadata(...)
```

### C.2 JSON 从哪来：不是拼字符串，是整份对象

底层 `_parse_llm_chunk`（`dsl/slim/llm.py:607-652`）：
```python
if expect_structured_output:
    structured_output = _STRUCTURED_OUTPUT_ADAPTER.validate_python(chunk.get("structured_output"))
    return LLMResultChunkWithStructuredOutput(..., structured_output=structured_output)
```

`_STRUCTURED_OUTPUT_ADAPTER` 是 `TypeAdapter(dict | None)`（`llm.py:53`）——它期望 `chunk["structured_output"]` **已经是解析好的 dict**，不是 JSON 字符串碎片。**"拼半个 JSON"发生在 slim 二进制内部（仓库外），graphon 收到时已是完整对象。**

### C.3 累积策略：覆盖保留最后一份

`_StructuredOutputAccumulator`（`dsl/slim/llm.py:68-84`）对 JSON 用**覆盖**（不像文本用 append）：
```python
def consume(self, structured_output):
    if structured_output is None: return             # None 忽略
    self.structured_output = structured_output        # 后来的覆盖先前的
    self.has_structured_output = True

def finalize(self, *, expect_structured_output):
    if self.has_structured_output: return self.structured_output
    if expect_structured_output:                      # 说好要却没拿到 → 报错
        raise SlimStructuredOutputParseError(...)
```

### C.4 两层校验

| 层 | 谁做 | 校验什么 | 位置 |
|---|---|---|---|
| 模型运行时层 | `_STRUCTURED_OUTPUT_ADAPTER.validate_python` | JSON 格式合法（能否解析成 dict） | `dsl/slim/llm.py:634` |
| 节点层 | `Draft7Validator(json_schema).validate` | 符合业务 schema（字段/类型对不对） | `nodes/llm/node.py:1090-1113` |

分工原因：SlimLLM 是通用运行时，不知道业务要什么结构，只能保证语法；只有 LLMNode 持有业务 schema，能做语义校验。

### C.5 文本 vs JSON 处理哲学对比

| | 文本 (text) | 结构化 (JSON) |
|---|---|---|
| chunk 里是什么 | 一小段文本片段 | 一份完整 dict（或 None） |
| 累积方式 | append 拼接（`node.py:1014`） | 覆盖保留最后一份（`llm.py:77`） |
| 会不会"半个" | 会（跨 chunk 断裂，靠 `_hold`） | 不会（每份都是完整快照） |

---

## 附录 D：SlimClient——底层模型适配器的进程通信

正文第 12 节说插件类节点委托给适配器，这里展开 `SlimLLM` 底下的 `SlimClient` 到底怎么跟模型通信。

### D.1 关键事实：slim 真正实现不在本仓库

`dify-plugin-daemon-slim` 是一个**外部 Go 可执行文件**（来自 dify-plugin-daemon 项目）。本仓库 `src/graphon/dsl/slim/` 只有"客户端"代码：

| 文件 | 职责 |
|---|---|
| `client.py` | 进程通信层：拉起子进程、stdin/stdout、事件解析、二进制定位 |
| `llm.py` | LLM 语义层：chunk 解析、结构化输出、三层分叉 |
| `config.py` | 配置：环境变量、路径、超时 |
| `package_loader.py` | 插件包加载：marketplace 下载/缓存、模型 schema 解析 |

### D.2 二进制定位（`client.py:291-311`）

```python
def resolve_slim_binary_path():
    configured_path = os.environ.get("SLIM_BINARY_PATH", "").strip()   # ① 优先环境变量
    if configured_path: return ...（校验存在+可执行）
    binary_path = shutil.which("dify-plugin-daemon-slim")               # ② 否则在 PATH 找
    if binary_path is None:
        raise ...("Set SLIM_BINARY_PATH to override it.")
```

### D.3 进程边界：请求进、事件出（`client.py:131-176`）

这是 graphon 看得到的最底层。过了 `Popen` 就是黑盒：
```python
def invoke_events(self, *, plugin_id, action, data):
    process = subprocess.Popen(
        [binary, "-id", plugin_id, "-action", action],
        stdin=PIPE, stdout=PIPE, stderr=stderr_file, text=True, env=config.build_env())
    process.stdin.write(json.dumps({"data": ...}))    # 请求 → stdin (一行 JSON)
    process.stdin.close()
    yield from _iter_slim_events(process.stdout)       # 响应 ← stdout (NDJSON)
    _check_slim_process_exit(process, stderr_file)     # 检查退出码
```

### D.4 NDJSON 协议：为什么 graphon 里不会有"半个 JSON"

`_iter_slim_events`（`client.py:383-390`）按**行**迭代，`parse_slim_event`（`client.py:393-427`）对整行 `json.loads`：
```python
for line in stdout:                       # ← 靠换行符切分, 一行 = 一个完整 JSON
    event = parse_slim_event(line)
    yield event
```
```python
def parse_slim_event(line):
    event = json.loads(line)              # 整行解析, 不完整就抛 JSONDecodeError
    match event.get("event"):
        case "message": ...   # 日志/进度
        case "chunk":   return SlimChunkEvent(data=event.get("data"))   # 数据
        case "done":    return SlimDoneEvent()                           # 结束
        case "error":   raise SlimClientError(...)                       # 错误
```

三层完整性保障：
1. 模型 SSE 字节碎片/半个 JSON → **slim 二进制内部**拼成完整对象（仓库外）
2. stdout 行读取 → Python `for line in stdout` 保证读到完整一行（等到 `\n`）
3. 行 → 事件 → `json.loads` 整行，不完整直接报错

### D.5 chunk 粒度：1:1 透传，不合并不重切

模型一次输出 = 一个 chunk = 一个 `StreamChunkEvent`，全程 `for...yield` 一进一出：
```
模型 SSE 帧 → slim 拼完整 JSON 写一行 → SlimChunkEvent → LLMResultChunk → StreamChunkEvent
```
graphon 不定义粒度，忠实转发；粒度由模型 API 决定。

### D.6 local vs remote 模式（`client.py:194-199`）

```python
case SlimChunkEvent():
    yield (_unwrap_remote_daemon_payload(event.data)
           if self.config.mode == "remote" else event.data)
```
- `local`：slim 子进程直接对接模型，chunk 是原始 payload
- `remote`：slim 连远程 daemon，payload 多一层包装，需 unwrap

---

## 附录 E：错误处理与重试

`NodeRunFailedEvent` 走 `EventHandler`（`event_handlers.py:248-276`）交给 `ErrorHandler.handle_node_failure`（`graph_engine/error_handler.py`）决策：

```python
result = frame.error_handler.handle_node_failure(frame_id=..., event=event)
if result is not None:
    self._dispatch_event(frame_id=..., event=result)   # 派生事件(retry/exception)继续处理
else:
    # 无补救: 容器帧记失败 or 整图 fail
    self._graph_execution.fail(RuntimeError(event.error))
    frame.state_manager.finish_execution(event.node_id)
```

### E.1 三种错误策略

节点 `error_strategy` 决定 `NodeRunExceptionEvent` 如何处理（`event_handlers.py:278-293`）：

| 策略 | 行为 | follow_branch |
|---|---|---|
| `DEFAULT_VALUE` | 用默认值继续，当作成功走正常出边 | False |
| `FAIL_BRANCH` | 走"失败分支"边（节点被提升为 BRANCH 类型） | True |
| （无策略） | 整个失败事件走 fail 流程 | — |

`FAIL_BRANCH` 节点在建图时被 `_promote_fail_branch_nodes`（`graph/graph.py:206-214`）提升为 `NodeExecutionType.BRANCH`，从而能像分支节点一样选边。

### E.2 重试

`NodeRunRetryEvent`（`event_handlers.py:295-316`）：
```python
node_execution.increment_retry()                        # retry_count++
frame.state_manager.finish_execution(event.node_id)     # 结束上一次尝试
self._collect(frame=frame, event=event)                 # 发 retry 事件给观察者
frame.state_manager.enqueue_node(event.node_id)         # 重新入队执行
```

注意 `NodeRunStartedEvent` 处理里（`event_handlers.py:207-212`）：只有首次尝试（`retry_count == 0`）才 collect Started 事件，重试保持静默，避免观察者看到重复的"开始"。

### E.3 Worker 层的兜底

若节点执行时抛出未被 `node.run()` 内部捕获的异常，Worker（`worker.py:142-161`）兜底造一个 `NodeRunFailedEvent`（`_build_fallback_failure_event`, `worker.py:329-352`）投进队列。这是双保险——业务异常在 `node.run()` 内已转失败事件（`node.py:665-667`），这里防的是框架级异常。

---

## 附录 F：WorkerPool 动态伸缩

`WorkerPool`（`graph_engine/worker_management/worker_pool.py:27`）根据图规模和队列积压动态调整 Worker 数量。

### F.1 初始 Worker 数（`worker_pool.py:77-91`）

```python
node_count = len(root_graph.nodes)
if node_count < 10:        initial = min_workers
elif node_count < 50:      initial = min(min_workers + 1, max_workers)
else:                      initial = min(min_workers + 2, max_workers)
```

### F.2 运行中扩缩容（`worker_pool.py:261-278`）

Dispatcher 每轮循环调 `check_and_scale`：
```python
queue_depth = ready_queue.qsize()
active_count = sum(w.has_current_task for w in workers)
idle_count = sum(w.is_idle for w in workers)
self._try_scale_up(queue_depth, current_count, active_count)     # 队列积压 → 加 Worker
self._try_scale_down(queue_depth, current_count, active_count, idle_count)  # 有空闲 → 减 Worker
```

- **扩容**（`worker_pool.py:171-202`）：`backlog = queue_depth - available` 超过阈值且没到 max_workers → 加一个。
- **缩容**（`worker_pool.py:204-259`）：有 Worker 空闲超过 `scale_down_idle_time` 且缩容后仍满足 min_workers → 移除。

### F.3 原子领取任务

多个 Worker 用 `task_claim_lock` + `task_claiming` 事件保证同一个任务只被一个 Worker 领取（`worker.py:128-141`）：
```python
with self._task_claim_lock:
    if not self._task_claiming.is_set(): return
    try:
        task = self._ready_queue.get(timeout=0)
    except queue.Empty:
        task_claimed = False
    else:
        self._has_current_task.set()
        task_claimed = True
```

---

## 附录 G：filters 层——ResponseStreamFilter 细节

正文第 14 节概述了 filters 层，这里展开 `ResponseStreamFilter`（`graph_engine/filters/response_stream.py`）如何把乱序事件整理成有序响应流。

### G.1 核心数据结构

| 结构 | 作用 | 位置 |
|---|---|---|
| `ResponseSession` | 一个响应节点的"流式游标"，持有模板 + 当前 index | `response_stream.py:46-66` |
| `Path` | 阻塞边集合，决定何时轮到某响应节点开始 stream | `response_stream.py:32-43` |
| `StreamBuffers` | 按 selector 分桶缓冲 chunk + 读游标 | `response_stream.py:90-188` |
| `_active_session` / `_waiting_sessions` | 当前正在输出的会话 + 排队会话 | `response_stream.py:220-221` |

### G.2 阻塞边（Path）——决定"何时轮到某响应节点"

初始化时（`_build_paths_map`, `response_stream.py:428-438`）枚举从 root 到响应节点的所有路径，筛出**阻塞边**（源节点是 BRANCH/CONTAINER/RESPONSE 或会阻塞变量输出，`_is_blocking_edge`, `:490-501`）。只有某条路径的阻塞边全部 TAKEN、`Path.is_empty()` 成立，这个响应节点才允许开始 streaming。

### G.3 事件处理主循环（`response_stream.py:250-273`）

```python
def on_event(self, event):
    match event:
        case GraphRunStartedEvent():     激活无阻塞边即可开始的会话
        case NodeRunStreamChunkEvent():  若 selector 被引用 → 存 buffer 再 _try_flush; 否则丢弃/透传
        case GraphEdgeTakenEvent():      从各 Path 移除该边, 路径清空则激活对应会话
        case NodeRunSucceededEvent():    先 _try_flush 再放行原事件
```

### G.4 _try_flush——按模板顺序吐出（`response_stream.py:742-775`）

对 `_active_session`，从当前 index 顺序消费模板段：
- **文本段** → 直接生成 chunk 事件，index++
- **变量段** → 从 `StreamBuffers` 把已缓冲的 chunk 依序吐；源已 close 或变量池已有终值则该段完成 index++，否则 break 等更多 chunk
- 整个模板消费完 → `_end_session`，把队首的 waiting session 提为 active 并递归 flush

这样即使底层多节点并行、chunk 乱序，对外也是"一个响应节点接一个、段内按模板顺序"的干净增量流。

### G.5 可恢复

`ResponseStreamFilter` 实现 `dumps()/loads()`（`response_stream.py:279-314`），把 active/waiting session、paths_map、stream buffers 全序列化，配合引擎暂停/恢复，保证跨暂停的流式连续性。

---

## 附录 H：节点类型如何映射到节点类

正文提到 DSL 导入时按 node_type 造节点，这里给机制。

### H.1 注册表 + 工厂

`SlimDslNodeFactory.NODE_BUILDERS`（`dsl/node_factory.py:837-856`）是一张 `节点类型 → 建造函数` 的字典：
```python
NODE_BUILDERS = {
    BuiltinNodeTypes.START:   _create_start_node,
    BuiltinNodeTypes.IF_ELSE: _create_if_else_node,
    BuiltinNodeTypes.LLM:     _create_llm_node,
    ...
}
```

### H.2 create_node 查表（`dsl/node_factory.py:449-454`）

```python
def create_node(self, node_config):
    request = self._node_request(node_config)         # 从 data.type 取 node_type
    builder = self.NODE_BUILDERS.get(request.node_type)
    if builder is None:
        raise self._unsupported_node_error(request)   # 表里没有 → 报错
    return builder(self, request)                      # 建造函数实例化具体节点类
```

`Graph.init` 对配置里每个节点都调一次 `create_node`（`graph/graph.py:149-167`）。`Graph` 只依赖抽象的 `NodeFactory` 协议（`graph/graph.py:30-41`），不与任何具体节点耦合。

### H.3 两条建图路径

| 路径 | 节点从哪来 | 用 NODE_BUILDERS? |
|---|---|---|
| DSL 导入（`loads()`） | YAML 的 `type` 字段查表 | 是 |
| Python 手写（`Graph.new()`） | 直接 `IfElseNode(...)` new | 否，手动实例化 |

每个节点类也用类属性自我声明类型，如 `IfElseNode`（`nodes/if_else/if_else_node.py:29-30`）：
```python
class IfElseNode(Node[IfElseNodeData]):
    node_type = BuiltinNodeTypes.IF_ELSE
    execution_type = NodeExecutionType.BRANCH     # 声明是分支节点 → Dispatcher 走 handle_branch_completion
```

---

# 附录 I：术语表

集中定义全文出现的关键术语，按主题分组。

## I.1 状态与标记

| 术语 | 定义 | 出现位置 |
|---|---|---|
| `UNKNOWN` | 边/节点的初始状态——上游还没跑完，尚未确定走不走 | `enums.NodeState` |
| `TAKEN` | 边被"走过"——上游成功且选择了这条路；节点被"占用"准备执行 | `graph_state_manager.py:59, 113` |
| `SKIPPED` | 边/节点被"跳过"——上游被跳过或分支未选中它 | `skip_propagator.py:87, 115` |
| `NodeExecutionType` | 节点的执行类型：`ROOT`(根) / `BRANCH`(分支) / `RESPONSE`(响应) / `CONTAINER`(容器) / 普通 | `enums.py` |
| `_unfinished_nodes` | StateManager 里的集合，记录"还没跑完的节点"，为空即全图完成 | `graph_state_manager.py:44` |

## I.2 任务与事件

| 术语 | 定义 | 出现位置 |
|---|---|---|
| `StartTask` | ready_queue 里的任务：让 Worker 从头执行某节点 | `ready_queue/protocol.py` |
| `ResumeTask` | ready_queue 里的任务：让 Worker 恢复某个挂起的容器节点 | `ready_queue/protocol.py` |
| `TaskEvent` | event_queue 里的载荷：`(frame_id, event)`，Worker 投给 Dispatcher | `entities/tasks.py:9` |
| `ContainerAwaitTask` | event_queue 里的载荷：容器节点请求跑子图的信号 | `entities/tasks.py:15` |
| `ContainerAwaitRequest` | 节点 yield 出的"我要跑子图"请求（LoopFrameRequest/IterationFrameRequest） | `nodes/container_effects.py:75` |
| `NodeRunStartedEvent` 等 | 节点执行产出的图级事件（Started/StreamChunk/Succeeded/Failed/…） | `graph_events/node.py` |

## I.3 帧与容器

| 术语 | 定义 | 出现位置 |
|---|---|---|
| `frame_id` | 执行帧的标识；根图是 `ROOT_FRAME_ID`，每个容器子图有自己的 frame_id | `ready_queue/protocol.py` |
| `ExecutionFrame` | 一套图执行环境的打包（graph + StateManager + EdgeProcessor + ErrorHandler） | `frames.py:29` |
| `ROOT_FRAME_ID` | 根图（最外层图）的固定 frame_id | `ready_queue/protocol.py` |
| `invocation_id` | 一次容器调用的唯一身份证（UUID），用于挂起/恢复配对 | `worker.py:272` |
| `ContainerRunState` | 挂起时存的"待恢复凭据"（记住哪个容器、跑到第几轮） | `runtime/container_state.py` |
| `ContainerFrameState` | 子帧的可序列化快照（暂停恢复用） | `runtime/container_state.py` |

## I.4 上下文与状态载体

| 术语 | 定义 | 出现位置 |
|---|---|---|
| `GraphInitParams` | 静态上下文，整次执行不变的只读配置 | `entities/graph_init_params.py:9` |
| `GraphRuntimeState` | 运行时上下文，全局共享的可变账本 | `runtime/graph_runtime_state.py:223` |
| `VariablePool` | 数据上下文，节点间传数据的键值存储 `(node_id,var)→value` | `runtime/variable_pool.py` |
| `GraphExecution` | 整图生命周期状态机（started/completed/paused/aborted/error） | `domain/graph_execution.py:80` |
| `NodeExecution` | 单节点执行记录（execution_id + retry_count） | `domain/node_execution.py:6` |
| `execution_id` | 一次节点执行的唯一 ID（区分同节点的多次执行/重试） | `domain/node_execution.py:14` |

## I.5 LLM 与插件

| 术语 | 定义 | 出现位置 |
|---|---|---|
| `chunk` | 模型一次输出对应的一个数据片段（一个 StreamChunkEvent） | `nodes/llm/node.py:1018` |
| `full_text_buffer` | LLM 节点内累加完整文本的 StringIO 缓冲区 | `nodes/llm/node.py:1014` |
| `structured_output` | 模型按 json_schema 返回的结构化 JSON 对象 | `dsl/slim/llm.py:632` |
| `reasoning_format` | 推理内容处理模式：`tagged`(保留标签) / `separated`(分流) | `nodes/llm/reasoning.py` |
| `LLMProtocol` | 节点依赖的模型运行时接口，由 SlimLLM 等实现 | `nodes/llm/runtime_protocols.py` |
| `ToolNodeRuntimeProtocol` | 工具节点依赖的运行时接口，由外部适配器注入 | `nodes/runtime.py:19` |
| NDJSON | slim 与 graphon 约定的协议：每行一个完整 JSON 对象 | `dsl/slim/client.py:384` |

---

# 附录 J：五层组件总览大图

把所有组件按五层画进一张图，标出主要的调用/数据关系。

```
════════════════════════════════════════════════════════════════════════════════════
  调用方 (examples/slim_llm/code.py, dsl.py)
        │ engine.run() → Generator[GraphEngineEvent]
        ▼
┌──────────────────────────────── 编排层 ─────────────────────────────────────────────┐
│                                                                                      │
│   GraphEngine ──装配所有子系统, run() 生成器出口, layer() 挂钩子                        │
│      │                                                                               │
│      ├─── WorkerPool ───┬── Worker-0 ┐                                                │
│      │  (动态伸缩)        ├── Worker-1 ┼─ 抢 ready_queue, node.run(), 投 event_queue     │
│      │                   └── Worker-N ┘   (多线程, 只执行不决策)                         │
│      │                                                                               │
│      └─── Dispatcher (单线程大脑) ─── 消费 event_queue, 查状态机判停, 调 EventHandler   │
│                 │                                                                    │
│                 └── CommandProcessor ◀── CommandChannel (abort/pause/update_vars)     │
└──────────┬─────────────────────────────────┬───────────────────────────────────────┘
           │ ①任务流                          │ ②事件流
           ▼                                 ▼
┌──────────────────────── 状态层 (共享账本) ─────────────────────────────────────────┐
│                                                                                    │
│   GraphRuntimeState ─────────────────────────────────────────────────────┐        │
│      ├── ready_queue          (StartTask/ResumeTask, Worker 领活)          │        │
│      ├── deferred_ready_queue (暂停冷藏柜)                                  │        │
│      ├── VariablePool         (③数据流: (node,var)→value 节点间桥)          │        │
│      ├── llm_usage/outputs/node_run_steps                                  │        │
│      ├── container_runs/container_frames (容器挂起状态)                      │        │
│      └── GraphExecution (状态机: started/paused/aborted/error)             │        │
│               └── NodeExecution×N (execution_id + retry_count)             │        │
│                                                                            │        │
│   GraphInitParams (静态只读: workflow_id/graph_config/call_depth) ─────────┘        │
└──────────┬─────────────────────────────────────────────────────────────────────────┘
           │ EventHandler._complete_node 调用
           ▼
┌──────────────────────── 推进层 (每个 ExecutionFrame 一套) ──────────────────────────┐
│   GraphStateManager ── enqueue_node / is_node_ready / is_execution_complete         │
│   EdgeProcessor     ── process_node_success / handle_branch_completion (算下游)      │
│   SkipPropagator    ── 未选中分支路径标 SKIPPED 并递归传播                            │
│   ErrorHandler      ── handle_node_failure (retry/fail-branch/整图失败)             │
└──────────┬───────────────────────────────────────────────────────────────────────┘
           │ FrameRegistry.get(frame_id) 路由
           ▼
┌──────────────────────── 帧/容器层 ─────────────────────────────────────────────────┐
│   FrameRegistry ── frame_id → ExecutionFrame 路由表                                  │
│   ExecutionFrame ── (graph, runtime_state, state_manager, edge_processor, err_hdlr)  │
│   ContainerHandler (Loop/Iteration) ── 造子帧跑子图, 挂起/恢复                         │
│        materialize_child_frame → 子帧(独立推进组件, 共享 ready_queue/状态机)           │
└──────────┬───────────────────────────────────────────────────────────────────────┘
           │ 节点执行
           ▼
┌──────────────────────── 节点层 ────────────────────────────────────────────────────┐
│   Node.run() (模板方法) → _run() (子类实现)                                           │
│     ├─ StartNode/EndNode/AnswerNode (一次性 NodeRunResult)                            │
│     ├─ IfElseNode (BRANCH, 选边)                                                     │
│     ├─ CodeNode (一次性, 调 code_executor)                                            │
│     ├─ LLMNode (流式, 注入 LLMProtocol=SlimLLM) ──┐                                   │
│     ├─ ToolNode (流式, 注入 ToolNodeRuntimeProtocol) ─┤ 协议注入                       │
│     └─ LoopNode/IterationNode (CONTAINER, yield ContainerAwaitRequest)               │
│                                                    ▼ 委托适配器                       │
│                              SlimLLM → SlimClient → subprocess(slim二进制) → 模型API   │
└──────────┬───────────────────────────────────────────────────────────────────────┘
           │ 引擎产出原始事件流 (并行乱序)
           ▼
┌──────────────────────── 事件/输出层 ───────────────────────────────────────────────┐
│   EventManager ── 缓存事件 + 主线程 emit_events() 流式 yield + 通知 Layers           │
│   EventHandler ── @singledispatchmethod 按事件类型分派消费                            │
│   Layers ── on_graph_start/end, on_node_run_start/end, on_event (日志/限流钩子)      │
│        │                                                                            │
│        ▼ (引擎外, 可选)                                                              │
│   filter_graph_events + ResponseStreamFilter ── 把乱序事件整理成有序响应流           │
└────────────────────────────────────────────────────────────────────────────────────┘
════════════════════════════════════════════════════════════════════════════════════
```

---

# 附录 K：复杂工作流完整流转 trace（分支 + 多入边）

正文第 11 节 trace 了线性图。这里用一个**带分支和汇聚**的图，完整展示条件分支、跳过传播、多入边 join 的流转。

## K.1 图结构

```
                    ┌─(e1, handle=true)─▶ llm_a ─(e3)─┐
start ─(e0)─▶ cond ─┤                                 ├─▶ answer
                    └─(e2, handle=false)─▶ llm_b ─(e4)─┘
```

- `cond` 是 if-else 分支节点（BRANCH）
- `answer` 是多入边节点（入边 e3, e4）
- 假设 `cond` 判定结果选中 `true` 分支（走 llm_a）

初始所有边 = `UNKNOWN`，`_unfinished_nodes = {}`。

## K.2 完整时间轴

| 时刻 | 谁 | 动作 | 边/节点状态变化 | `_unfinished_nodes` |
|---|---|---|---|---|
| t0 | 引擎 | `enqueue_node("start")` | start=TAKEN | {start} |
| t1 | Worker | 执行 start，产出 Started/Succeeded | — | {start} |
| t2 | Dispatcher | `_complete_node(start)`：写 VariablePool；`e0`→TAKEN；`is_node_ready(cond)`=True | e0=TAKEN | {start} |
| t3 | Dispatcher | `enqueue_node("cond")`；`finish_execution(start)` | cond=TAKEN | {cond} |
| t4 | Worker | 执行 cond（if-else），判定 true，产出 Succeeded(edge_source_handle="true") | — | {cond} |
| t5 | Dispatcher | `_complete_node(cond, follow_branch=True)` → `handle_branch_completion("cond","true")` | — | {cond} |
| t5a | EdgeProcessor | `categorize_branch_edges`：选中=[e1], 未选中=[e2] | — | {cond} |
| t5b | SkipPropagator | `skip_branch_paths([e2])`：`e2`→SKIPPED | e2=SKIPPED | {cond} |
| t5c | SkipPropagator | `propagate_skip_from_edge(e2)`：llm_b 入边全 SKIPPED → llm_b=SKIPPED | llm_b=SKIPPED | {cond} |
| t5d | SkipPropagator | llm_b 出边 `e4`→SKIPPED；检查 answer：入边 e3=UNKNOWN → has_unknown, 停 | e4=SKIPPED | {cond} |
| t5e | EdgeProcessor | 处理选中边 e1：`e1`→TAKEN；`is_node_ready(llm_a)`=True | e1=TAKEN | {cond} |
| t6 | Dispatcher | `enqueue_node("llm_a")`；`finish_execution(cond)` | llm_a=TAKEN | {llm_a} |
| t7 | Worker | 执行 llm_a（流式），产出 Started + chunks + Succeeded | — | {llm_a} |
| t8 | Dispatcher | `_complete_node(llm_a)`：写 VariablePool[(llm_a,text)]；`e3`→TAKEN | e3=TAKEN | {llm_a} |
| t8a | EdgeProcessor | `is_node_ready(answer)`：入边 e3=TAKEN, e4=SKIPPED → 无 UNKNOWN 且有 TAKEN → **True** | — | {llm_a} |
| t9 | Dispatcher | `enqueue_node("answer")`；`finish_execution(llm_a)` | answer=TAKEN | {answer} |
| t10 | Worker | 执行 answer（RESPONSE），读 VariablePool[(llm_a,text)] 填模板，产出 Succeeded | — | {answer} |
| t11 | Dispatcher | `_complete_node(answer)`：merge_response_outputs；answer 无出边；`finish_execution(answer)` | — | {} |
| t12 | Dispatcher | `is_execution_complete()`=True → complete() → mark_complete() | — | {} |
| t13 | 主线程 | emit 剩余事件 → `GraphRunSucceededEvent` | — | — |

## K.3 关键观察

- **t5d 是多入边确定的关键前奏**：跳过传播到 e4 后检查 answer，但 e3 还是 UNKNOWN → **暂停，不做处理**。answer 命运未定。
- **t8a 才真正确定 answer**：llm_a 成功把 e3 标 TAKEN，此刻 answer 入边 = {TAKEN, SKIPPED}，无 UNKNOWN 了 → 正向 `is_node_ready` 触发就绪。
- **answer 只被 enqueue 一次**：无论从跳过路径(反向)还是成功路径(正向)，都由"最后一条 UNKNOWN 入边被敲定"的那个动作触发，不会重复。
- 若 cond 选了 false（走 llm_b），则对称——e1 被 SKIPPED、llm_a 被跳过、e3 被 SKIPPED，answer 靠 e4=TAKEN 就绪。

---

# 附录 L：容器节点(loop)分步 trace

展示一个 loop 节点跑 2 轮子图的完整挂起/恢复流转。

## L.1 图结构

```
主图:  start ─▶ loop ─▶ end
loop 内部子图:  loop_start ─▶ process ─▶ loop_end   (跑 2 轮后 break)
```

## L.2 完整时间轴

| 时刻 | 谁 | 动作 | 关键状态 |
|---|---|---|---|
| t0 | 引擎 | enqueue start → 执行 → 成功 → enqueue loop | 主帧(ROOT) |
| t1 | Worker | 领 loop 的 StartTask，`node.run()` 执行到需要跑子图 | ROOT 帧 |
| t2 | loop节点 | yield `LoopFrameRequest`(轮1) | — |
| t3 | Worker | `_consume_node_events` 见 ContainerAwaitRequest：生成 `invocation_id=X`，`put_container_run(X, ...)`，投 `ContainerAwaitTask(X)`，`return None,True`(挂起) | container_runs={X} |
| t4 | Worker | loop 节点冻结，Worker 去领别的活；**不发 on_node_run_end** | — |
| t5 | Dispatcher | 收到 ContainerAwaitTask(X) → `EventHandler.start_container` → `LoopContainerHandler.start_await(X)` | — |
| t6 | Handler | `_loop_break_conditions_reached`? 否 → `_start_loop_frame` | — |
| t7 | Handler | `FrameRegistry.materialize_child_frame`：造子帧(frame_id=F1, 独立 StateManager/EdgeProcessor, 共享 ready_queue) | 帧 F1 注册 |
| t8 | Handler | 子图起点 loop_start `enqueue_node` 到共享 ready_queue（带 frame_id=F1） | ready_queue+1 |
| t9 | Worker | 领 F1 的任务，执行 loop_start → process → loop_end（完全复用主图机制，事件带 frame_id=F1） | 子帧内流转 |
| t9a | Dispatcher | 处理 F1 的事件时 `frame_registry.get(F1)` 拿子帧组件；`prepare_frame_event` 打上 loop_id/loop_index=0 | — |
| t10 | Handler | 子图轮1 跑完 → 产出 `ResumeTask(X, result=轮1结果)` 塞回 ready_queue | ready_queue+1 |
| t11 | Worker | 领 ResumeTask(X)：`get_container_run(X)` 找回 loop 节点，调 `node.resume_container(result=轮1结果)` | — |
| t12 | loop节点 | 处理轮1结果，未达 break → yield `LoopFrameRequest`(轮2) | — |
| t13 | Worker | 又见 ContainerAwaitRequest：**复用 invocation_id=X**（不新建），投 ContainerAwaitTask(X)，挂起 | container_runs={X} |
| t14 | Handler | start_await(X) 又跑一轮子图（frame F2 或复用），loop_index=1 | 帧 F2 |
| t15 | Worker | 子图轮2 执行完 → ResumeTask(X, 轮2结果) | — |
| t16 | loop节点 | resume_container 处理轮2结果，**达到 break 条件** → yield 最终 `NodeRunSucceededEvent`(聚合 outputs) | — |
| t17 | Worker | 这次没有 ContainerAwaitRequest，正常跑完，`pop_container_run(X)` 清理凭据，发 on_node_run_end | container_runs={} |
| t18 | Dispatcher | `_complete_node(loop)`：写 outputs；loop 出边→TAKEN；enqueue end | — |
| t19 | ... | end 执行 → 全图完成 | {} |

## L.3 关键观察

- **挂起 = 提前 return + 存凭据**（t3）：loop 节点没跑完就交回控制权，`ContainerRunState` 是恢复的唯一依据。
- **子图 = 完全复用主图机制**（t9）：子图节点进同一个 ready_queue、被同一批 Worker 执行、走同样的就绪判定，只是事件带 F1/F2 的 frame_id，用 FrameRegistry 路由到子帧的推进组件。
- **循环 = 挂起↔恢复的多次往返**（t2→t16）：每轮一次 yield-request/resume，`invocation_id` 全程不变（t13 复用），保证是同一次容器调用。
- **on_node_run_start/end 各一次**：start 在 t1 首次执行时发，end 在 t17 真正结束时发；中间所有挂起都因 `suspended=True` 跳过 end 钩子（见附录 A 外层逻辑 `worker.py:251`）。
- **break 判定有两处**：`start_await` 开头（t6，进入前判断）和 `resume_container` 内（t16，每轮结束后判断）。

---

*本文档基于对 graphon 源码的逐步分析整理，所有 `文件:行号` 引用以当前仓库状态为准。*
