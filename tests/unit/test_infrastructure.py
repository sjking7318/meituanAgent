from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from sales_assistant.domain import ConversationBusyError
from sales_assistant.infrastructure.model_gateway.gateway import MockModelGateway
from sales_assistant.infrastructure.redis.event_stream import InMemoryRunEventStream
from sales_assistant.infrastructure.redis.lease import InMemoryLeaseManager

pytestmark = pytest.mark.asyncio


async def test_lease_grants_monotonic_fencing_tokens() -> None:
    manager = InMemoryLeaseManager()
    tenant, conv = uuid4(), uuid4()
    async with manager.hold(tenant, conv) as lease1:
        first = lease1.fencing_token
        await lease1.ensure_valid()
    async with manager.hold(tenant, conv) as lease2:
        assert lease2.fencing_token > first
    await manager.close()


async def test_lease_rejects_concurrent_hold() -> None:
    manager = InMemoryLeaseManager()
    tenant, conv = uuid4(), uuid4()
    async with manager.hold(tenant, conv):
        with pytest.raises(ConversationBusyError):
            async with manager.hold(tenant, conv):
                pass
    await manager.close()


async def test_event_stream_append_and_read() -> None:
    stream = InMemoryRunEventStream()
    run_id = uuid4()
    await stream.append(run_id, "run.started", {"a": 1})
    await stream.append(run_id, "message.completed", {"b": 2})
    events = await stream.read(run_id, after_id="0-0", block_milliseconds=0)
    assert [e.event_type for e in events] == ["run.started", "message.completed"]

    tail = await stream.read(run_id, after_id=events[0].id, block_milliseconds=0)
    assert [e.event_type for e in tail] == ["message.completed"]
    await stream.close()


async def test_event_stream_blocks_until_event() -> None:
    stream = InMemoryRunEventStream()
    run_id = uuid4()

    async def append_later() -> None:
        await asyncio.sleep(0.02)
        await stream.append(run_id, "run.started", {})

    task = asyncio.create_task(append_later())
    events = await stream.read(run_id, after_id="0-0", block_milliseconds=500)
    await task
    assert len(events) == 1
    await stream.close()


async def test_mock_gateway_echoes_prompt() -> None:
    from sales_assistant.domain import ModelRequest

    gateway = MockModelGateway()
    response = await gateway.generate(
        ModelRequest(
            system_prompt="sys",
            user_prompt="产品政策",
            conversation_id=uuid4(),
            run_id=uuid4(),
        )
    )
    assert "产品政策" in response.content
    assert response.model == "mock-synth"


async def test_mock_embedder_is_deterministic() -> None:
    from sales_assistant.infrastructure.model_gateway.embeddings import MockEmbedder

    embedder = MockEmbedder(dim=8)
    first = await embedder.embed(["销售话术", "商家画像"])
    second = await embedder.embed(["销售话术", "商家画像"])
    assert first == second
    assert len(first) == 2
    assert all(len(vec) == 8 for vec in first)


async def test_mock_reranker_orders_by_overlap() -> None:
    from sales_assistant.infrastructure.model_gateway.embeddings import MockReranker

    reranker = MockReranker()
    ranked = await reranker.rerank(
        "销售话术推荐",
        ["销售话术相关内容", "完全无关文本", "话术推荐要点"],
        top_k=2,
    )
    assert len(ranked) == 2
    assert ranked[0][1] >= ranked[1][1]


async def test_build_embedder_and_reranker_fall_back_to_mock() -> None:
    from sales_assistant.infrastructure.model_gateway.embeddings import (
        MockEmbedder,
        MockReranker,
        build_embedder,
        build_reranker,
    )
    from sales_assistant.settings import AppEnvironment, EmbeddingProvider, Settings

    settings = Settings(
        app_env=AppEnvironment.TEST,
        model_provider="mock",
        embedding_provider=EmbeddingProvider.MOCK,
        embedding_base_url=None,
        embedding_api_key=None,
    )
    assert isinstance(build_embedder(settings), MockEmbedder)
    assert isinstance(build_reranker(settings), MockReranker)
