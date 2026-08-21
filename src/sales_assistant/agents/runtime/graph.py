from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from sales_assistant.agents.runtime.state import RunState, initial_state
from sales_assistant.application.conversation_service import AgentOutcome
from sales_assistant.application.retrieval_service import RetrievalService
from sales_assistant.domain import (
    Evidence,
    MessageRole,
    ModelGateway,
    ModelRequest,
    ModelTurn,
    SkillLibrary,
)
from sales_assistant.infrastructure.observability.tracing import TraceHandlerFactory

logger = structlog.get_logger()

_NIL_UUID = UUID(int=0)

# Fixed, versioned worker prompt. Rebuilt from template every invocation so no
# persona leaks across workers or turns (agent-design.md 6/7).
_SUPERVISOR_SYSTEM_PROMPT = """\
你是意图与技能路由分析师。根据用户输入和“可用技能清单”，只输出一个标签：
- 若某个技能明显适用，输出 `skill:<技能名>`（技能名取自清单）。
- 否则从以下两个中选一个：chitchat（寒暄闲聊，或对之前对话内容的追问/回忆，
  如“你还记得我说过什么”）、knowledge_qa（其余所有需要知识、数据或业务支撑的问题，
  默认归此类）。
只输出标签本身，不要输出其他任何内容。
"""

_CHITCHAT_SYSTEM_PROMPT = """\
你是友好的销售助手。用简洁、礼貌的中文进行日常寒暄回复，或根据上文对话回答用户
对先前内容的追问（如复述用户之前提到的信息）。不要编造任何业务、产品或数据信息。
"""

_CLARIFY_ANSWER = (
    "我还不太确定你的具体需求。可以补充一下你想了解的产品、政策或业务场景吗？"
    "例如“某产品的佣金政策”或“某商家近30天的拜访数据”。"
)

_CHITCHAT_KEYWORDS = ("你好", "在吗", "谢谢", "hi", "hello", "早上好", "晚上好", "再见")

# Cheap keyword hints per skill for the deterministic fallback (keeps Mock-backed
# tests stable when the model does not return a usable label).
_SKILL_HINTS: dict[str, tuple[str, ...]] = {
    "visit-planning": ("拜访计划", "拜访规划", "拜访方案", "制定拜访", "拜访安排"),
    "visit-analysis": ("拜访历史", "拜访记录", "复盘拜访", "拜访情况", "拜访趋势"),
}


def _heuristic_skill(query: str, available: set[str]) -> str | None:
    for name, hints in _SKILL_HINTS.items():
        if name in available and any(h in query for h in hints):
            return name
    return None


def _heuristic_intent(query: str) -> str:
    text = query.strip().lower()
    if any(kw in text for kw in _CHITCHAT_KEYWORDS):
        return "chitchat"
    if not text or len(text) <= 2:
        return "clarify"
    return "knowledge_qa"

# Fixed, versioned worker prompt. Rebuilt from template every invocation so no
# persona leaks across workers or turns (agent-design.md 6/7).
_KNOWLEDGE_QA_SYSTEM_PROMPT = """\
你是销售智能助手的知识问答 Worker。严格遵守以下规则：
1. 只能使用下方“检索证据”中的事实回答，不得使用证据之外的知识。
2. 每条事实性结论后用 [编号] 标注来源，编号对应证据前的序号（如 [1]、[2]）。
3. 证据不足以回答时，明确说明“现有知识不足以回答”，禁止猜测。
"""

_ABSTAIN_ANSWER = (
    "现有知识库中没有检索到与该问题相关的证据，无法给出可靠回答。建议补充相关知识文档或换一种问法。"
)


def _format_evidence(evidence: Sequence[Evidence]) -> str:
    blocks = []
    for index, item in enumerate(evidence, start=1):
        header = f"[{index}]"
        if item.title:
            header += f" {item.title}"
        if item.section_path:
            header += f" / {item.section_path}"
        blocks.append(f"{header}\n{item.text}")
    return "\n\n".join(blocks)


def _citations_from(evidence: Sequence[Evidence]) -> list[dict[str, object]]:
    return [
        {
            "marker": index,
            "chunk_id": item.chunk_id,
            "document_version_id": item.document_version_id,
            "title": item.title,
            "section_path": item.section_path,
            "page": item.page,
        }
        for index, item in enumerate(evidence, start=1)
    ]


class AgentRuntime:
    """Supervisor -> Worker -> synthesize graph with Agentic RAG (rag-design.md).

    Domain logic stays framework-agnostic: nodes call injected ports/services.
    Compiled with a MySQL checkpointer so any instance can resume by run_id.
    """

    def __init__(
        self,
        model_gateway: ModelGateway,
        retrieval_service: RetrievalService,
        checkpointer: BaseCheckpointSaver[str],
        trace_handler_factory: TraceHandlerFactory | None = None,
        skill_library: SkillLibrary | None = None,
    ) -> None:
        self._model_gateway = model_gateway
        self._retrieval = retrieval_service
        self._trace_handler_factory = trace_handler_factory
        self._skill_library = skill_library
        self._graph = self._build().compile(checkpointer=checkpointer)

    def _build(self) -> StateGraph:
        graph: StateGraph = StateGraph(RunState)
        graph.add_node("supervisor", self._supervise)
        graph.add_node("retrieve", self._retrieve)
        graph.add_node("knowledge_qa", self._knowledge_qa)
        graph.add_node("chitchat", self._chitchat)
        graph.add_node("skill", self._skill)
        graph.add_node("clarify", self._clarify)
        graph.add_node("synthesize", self._synthesize)
        graph.add_edge(START, "supervisor")
        graph.add_conditional_edges(
            "supervisor",
            self._route,
            {
                "knowledge_qa": "retrieve",
                "chitchat": "chitchat",
                "skill": "skill",
                "clarify": "clarify",
            },
        )
        graph.add_edge("retrieve", "knowledge_qa")
        graph.add_edge("knowledge_qa", "synthesize")
        graph.add_edge("chitchat", "synthesize")
        graph.add_edge("skill", "synthesize")
        graph.add_edge("clarify", "synthesize")
        graph.add_edge("synthesize", END)
        return graph

    def _skill_names(self) -> set[str]:
        # Level-1: cheap catalog lookup (name + description only).
        if self._skill_library is None:
            return set()
        return {m.name for m in self._skill_library.catalog()}

    async def _supervise(self, state: RunState) -> RunState:
        # Intent Analyst persona lives only inside this node's context; the
        # label is validated and never leaks into worker prompts (agent-design 6).
        route = await self._classify(state["user_query"])
        return RunState(route=route, standalone_query=state["user_query"])

    def _route(self, state: RunState) -> str:
        route = state.get("route") or {}
        if route.get("skill"):
            return "skill"
        worker = route.get("primary_worker", "knowledge_qa")
        return worker if worker in {"knowledge_qa", "chitchat", "clarify"} else "knowledge_qa"

    async def _classify(self, query: str) -> dict[str, Any]:
        """Route to a skill (progressive disclosure) or a base intent.

        The level-1 skill catalog (name + description only) is injected into the
        classifier prompt so the model can pick a skill without ever loading its
        body. Falls back to a deterministic heuristic when the model output is
        unusable (also keeps Mock-backed tests stable).
        """
        available = self._skill_names()
        # Deterministic guard: a near-empty query is clarify regardless of the
        # model (the classifier is unreliable on 0-2 char inputs).
        if len(query.strip()) <= 2:
            return {"primary_worker": "clarify", "strategy": "heuristic"}
        catalog_text = ""
        if self._skill_library is not None:
            lines = [f"- {m.name}: {m.description}" for m in self._skill_library.catalog()]
            catalog_text = "可用技能清单：\n" + "\n".join(lines) + "\n\n" if lines else ""
        try:
            response = await self._model_gateway.generate(
                ModelRequest(
                    system_prompt=_SUPERVISOR_SYSTEM_PROMPT,
                    user_prompt=f"{catalog_text}用户输入：{query}\n只输出一个标签。",
                    conversation_id=_NIL_UUID,
                    run_id=_NIL_UUID,
                    history=(),
                )
            )
            label = response.content.strip().lower()
            if label.startswith("skill:"):
                skill = label.split(":", 1)[1].strip()
                if skill in available:
                    return {"skill": skill, "strategy": "supervised"}
            for candidate in ("knowledge_qa", "chitchat", "clarify"):
                if candidate in label:
                    return {"primary_worker": candidate, "strategy": "supervised"}
        except Exception:  # classification must never break the run
            logger.warning("intent_classification_failed_fallback_heuristic")
        matched = _heuristic_skill(query, available)
        if matched is not None:
            return {"skill": matched, "strategy": "heuristic"}
        return {"primary_worker": _heuristic_intent(query), "strategy": "heuristic"}

    async def _chitchat(self, state: RunState, config: RunnableConfig) -> RunState:
        # History-aware so multi-turn casual context (names, prior facts) carries
        # over; the assembled history already includes the rolling summary.
        response = await self._model_gateway.generate(
            ModelRequest(
                system_prompt=_CHITCHAT_SYSTEM_PROMPT,
                user_prompt=state["user_query"],
                conversation_id=UUID(state["conversation_id"]),
                run_id=UUID(state["run_id"]),
                history=_history_from_config(config),
            )
        )
        return RunState(
            answer=response.content,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            citations=[],
        )

    async def _clarify(self, _state: RunState) -> RunState:
        # Ambiguous query: ask a focused clarifying question, no external call.
        return RunState(
            answer=_CLARIFY_ANSWER,
            model="clarify",
            citations=[],
        )

    async def _skill(self, state: RunState, config: RunnableConfig) -> RunState:
        # Level-2: load the matched skill's SKILL.md body on demand and use it as
        # the worker's operating instructions. The body may reference level-3
        # resources; those are read by the model only if it asks (not eagerly).
        route = state.get("route") or {}
        skill_name = str(route.get("skill") or "")
        if self._skill_library is None or not skill_name:
            return RunState(answer=_CLARIFY_ANSWER, model="clarify", citations=[])
        try:
            loaded = self._skill_library.load(skill_name)
        except Exception:
            logger.warning("skill_load_failed", skill=skill_name)
            return RunState(answer=_CLARIFY_ANSWER, model="clarify", citations=[])

        resources_note = ""
        if loaded.resources:
            resources_note = (
                "\n\n可按需引用的资源文件（仅在需要时参考其内容）："
                + "、".join(loaded.resources)
            )
        system_prompt = (
            f"你正在执行技能「{loaded.name}」。严格遵循以下操作说明：\n\n"
            f"{loaded.instructions}{resources_note}"
        )
        response = await self._model_gateway.generate(
            ModelRequest(
                system_prompt=system_prompt,
                user_prompt=state["user_query"],
                conversation_id=UUID(state["conversation_id"]),
                run_id=UUID(state["run_id"]),
                history=_history_from_config(config),
            )
        )
        return RunState(
            answer=response.content,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            citations=[],
        )

    async def _retrieve(self, state: RunState) -> RunState:
        query = state.get("standalone_query") or state["user_query"]
        result = await self._retrieval.retrieve(
            tenant_id=UUID(state["tenant_id"]),
            query=query,
        )
        evidence = [
            {
                "chunk_id": e.chunk_id,
                "document_version_id": e.document_version_id,
                "text": e.text,
                "title": e.title,
                "section_path": e.section_path,
                "page": e.page,
            }
            for e in result.evidence
        ]
        return RunState(evidence=evidence)

    async def _knowledge_qa(self, state: RunState, config: RunnableConfig) -> RunState:
        evidence = [
            Evidence(
                chunk_id=e["chunk_id"],
                document_version_id=e["document_version_id"],
                text=e["text"],
                score=0.0,
                title=e.get("title"),
                section_path=e.get("section_path"),
                page=e.get("page"),
            )
            for e in state.get("evidence", [])
        ]
        # Evidence gate: no evidence => abstain, never guess (rag-design.md 5).
        if not evidence:
            return RunState(answer=_ABSTAIN_ANSWER, model="abstain")

        history = _history_from_config(config)
        prompt = (
            f"用户问题：{state['user_query']}\n\n"
            f"检索证据：\n{_format_evidence(evidence)}\n\n"
            "请依据上述证据回答，并按规则用 [编号] 标注来源。"
        )
        response = await self._model_gateway.generate(
            ModelRequest(
                system_prompt=_KNOWLEDGE_QA_SYSTEM_PROMPT,
                user_prompt=prompt,
                conversation_id=UUID(state["conversation_id"]),
                run_id=UUID(state["run_id"]),
                history=history,
            )
        )
        return RunState(
            answer=response.content,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            citations=_citations_from(evidence),
        )

    async def _synthesize(self, state: RunState) -> RunState:
        # Single-worker path: worker output is the answer. Multi-worker neutral
        # synthesis is a later milestone.
        return RunState(answer=state.get("answer"))

    async def run(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
        conversation_id: UUID,
        user_query: str,
        history: Sequence[ModelTurn] = (),
    ) -> AgentOutcome:
        state = initial_state(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            user_query=user_query,
        )
        config: RunnableConfig = {
            "configurable": {
                "thread_id": str(run_id),
                "checkpoint_ns": "",
                "tenant_id": tenant_id,
                # Transient context (not persisted in RunState checkpoints).
                "history": [(turn.role.value, turn.content) for turn in history],
            }
        }
        if self._trace_handler_factory is not None:
            handlers = self._trace_handler_factory.for_run(
                run_id=run_id,
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            if handlers:
                # Whole-graph trace; every node + LLM call nests as a child span.
                config["callbacks"] = handlers
        result = await self._graph.ainvoke(state, config=config)
        return AgentOutcome(
            answer=result.get("answer") or "",
            model=result.get("model") or "",
            input_tokens=result.get("input_tokens", 0),
            output_tokens=result.get("output_tokens", 0),
            citations=result.get("citations", []),
        )


def _history_from_config(config: RunnableConfig) -> tuple[ModelTurn, ...]:
    raw = config.get("configurable", {}).get("history", [])
    return tuple(ModelTurn(role=MessageRole(role), content=content) for role, content in raw)
