from __future__ import annotations

from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from sales_assistant.agents.runtime.graph import (
    _ABSTAIN_ANSWER,
    _CLARIFY_ANSWER,
    AgentRuntime,
    _heuristic_intent,
)
from sales_assistant.application.retrieval_service import RetrievalConfig, RetrievalService
from sales_assistant.infrastructure.milvus.memory import IndexedChunk, InMemoryRetriever
from sales_assistant.infrastructure.model_gateway.embeddings import MockEmbedder, MockReranker
from sales_assistant.infrastructure.model_gateway.gateway import MockModelGateway
from sales_assistant.infrastructure.skills import build_skill_library

pytestmark = pytest.mark.asyncio


def _runtime(retriever: InMemoryRetriever) -> AgentRuntime:
    service = RetrievalService(retriever, MockEmbedder(), MockReranker(), RetrievalConfig())
    gateway = MockModelGateway()
    library = build_skill_library()
    return AgentRuntime(gateway, service, InMemorySaver(), None, library)


async def test_graph_abstains_without_evidence() -> None:
    runtime = _runtime(InMemoryRetriever())
    outcome = await runtime.run(
        run_id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        conversation_id=uuid4(),
        user_query="产品政策是什么",
    )
    assert outcome.answer == _ABSTAIN_ANSWER
    assert outcome.model == "abstain"


async def test_graph_answers_with_evidence() -> None:
    tenant = uuid4()
    retriever = InMemoryRetriever()
    retriever.add(IndexedChunk("c1", "dv1", "p1", "产品政策：新商家首月免佣金", tenant))
    runtime = _runtime(retriever)
    outcome = await runtime.run(
        run_id=uuid4(),
        tenant_id=tenant,
        user_id=uuid4(),
        conversation_id=uuid4(),
        user_query="产品政策是什么",
    )
    assert outcome.answer != _ABSTAIN_ANSWER
    assert outcome.model == "mock-synth"


async def test_graph_routes_chitchat_to_worker() -> None:
    # MockModelGateway never emits a bare label, so classification deterministically
    # falls back to the heuristic; a greeting keyword routes to the chitchat worker.
    runtime = _runtime(InMemoryRetriever())
    outcome = await runtime.run(
        run_id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        conversation_id=uuid4(),
        user_query="你好，在吗",
    )
    # Chitchat worker calls the model (not the abstain/clarify constant paths).
    assert outcome.model == "mock-synth"
    assert outcome.answer not in {_ABSTAIN_ANSWER, _CLARIFY_ANSWER}
    assert outcome.citations == []


async def test_graph_routes_ambiguous_to_clarify() -> None:
    runtime = _runtime(InMemoryRetriever())
    outcome = await runtime.run(
        run_id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        conversation_id=uuid4(),
        user_query="?",
    )
    assert outcome.answer == _CLARIFY_ANSWER
    assert outcome.model == "clarify"
    assert outcome.citations == []


async def test_heuristic_intent_labels() -> None:
    assert _heuristic_intent("你好") == "chitchat"
    assert _heuristic_intent("谢谢啦") == "chitchat"
    assert _heuristic_intent("") == "clarify"
    assert _heuristic_intent("啊") == "clarify"
    assert _heuristic_intent("新商家首月的佣金政策是什么") == "knowledge_qa"


async def test_graph_routes_to_skill_worker() -> None:
    # Progressive disclosure: the visit-planning keyword matches a skill in the
    # catalog, so routing goes to the skill worker which loads SKILL.md (level-2)
    # and runs the model with it as instructions.
    runtime = _runtime(InMemoryRetriever())
    outcome = await runtime.run(
        run_id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        conversation_id=uuid4(),
        user_query="帮我为某餐饮商家制定一个拜访计划",
    )
    # Mock gateway answers the skill prompt (not the clarify/abstain constants).
    assert outcome.model == "mock-synth"
    assert outcome.answer not in {_ABSTAIN_ANSWER, _CLARIFY_ANSWER}
    assert outcome.citations == []
