from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from sales_assistant.application.memory_service import MemoryService
from sales_assistant.domain import Conversation, Message, MessageRole
from sales_assistant.infrastructure.model_gateway.gateway import MockModelGateway
from sales_assistant.infrastructure.mysql.database import Database
from sales_assistant.infrastructure.mysql.repositories import SqlUnitOfWorkFactory

pytestmark = pytest.mark.asyncio


async def _seed_turns(
    uow_factory: SqlUnitOfWorkFactory,
    tenant: UUID,
    conv_id: UUID,
    pairs: int,
) -> int:
    """Write `pairs` user/assistant message pairs; return last sequence."""
    seq = 0
    async with uow_factory() as uow:
        await uow.conversations.add(
            Conversation(id=conv_id, tenant_id=tenant, owner_id=uuid4())
        )
        for i in range(pairs):
            seq += 1
            await uow.messages.add(
                Message(
                    tenant_id=tenant,
                    conversation_id=conv_id,
                    role=MessageRole.USER,
                    content=f"用户问题{i}",
                    sequence=seq,
                )
            )
            seq += 1
            await uow.messages.add(
                Message(
                    tenant_id=tenant,
                    conversation_id=conv_id,
                    role=MessageRole.ASSISTANT,
                    content=f"助手回答{i}",
                    sequence=seq,
                )
            )
        await uow.commit()
    return seq


async def test_load_context_returns_recent_window(database: Database) -> None:
    uow_factory = SqlUnitOfWorkFactory(database.session_factory)
    tenant, conv = uuid4(), uuid4()
    last_seq = await _seed_turns(uow_factory, tenant, conv, pairs=6)
    memory = MemoryService(
        uow_factory, MockModelGateway(), recent_turns=2, summary_trigger_turns=4
    )
    turns = await memory.load_context(tenant, conv, before_sequence=last_seq + 1)
    # recent_turns=2 -> last 4 messages, no summary yet.
    assert len(turns) == 4
    assert turns[-1].content == "助手回答5"


async def test_maybe_summarize_creates_versioned_summary(database: Database) -> None:
    uow_factory = SqlUnitOfWorkFactory(database.session_factory)
    tenant, conv = uuid4(), uuid4()
    await _seed_turns(uow_factory, tenant, conv, pairs=8)  # 16 messages
    memory = MemoryService(
        uow_factory, MockModelGateway(), recent_turns=2, summary_trigger_turns=4
    )
    await memory.maybe_summarize(tenant, conv)

    async with uow_factory() as uow:
        summary = await uow.summaries.latest(tenant, conv)
    assert summary is not None
    assert summary.source_version == 1
    # keep last recent_turns*2=4 raw; summarize the other 12 -> covered_through=12.
    assert summary.covered_through_sequence == 12


async def test_summary_injected_into_context(database: Database) -> None:
    uow_factory = SqlUnitOfWorkFactory(database.session_factory)
    tenant, conv = uuid4(), uuid4()
    last_seq = await _seed_turns(uow_factory, tenant, conv, pairs=8)
    memory = MemoryService(
        uow_factory, MockModelGateway(), recent_turns=2, summary_trigger_turns=4
    )
    await memory.maybe_summarize(tenant, conv)

    turns = await memory.load_context(tenant, conv, before_sequence=last_seq + 1)
    # First turn is the injected summary (system role), then recent 4 raw turns.
    assert turns[0].role is MessageRole.SYSTEM
    assert "早期对话摘要" in turns[0].content
    assert len([t for t in turns if t.role is not MessageRole.SYSTEM]) == 4


async def test_maybe_summarize_noop_when_below_trigger(database: Database) -> None:
    uow_factory = SqlUnitOfWorkFactory(database.session_factory)
    tenant, conv = uuid4(), uuid4()
    await _seed_turns(uow_factory, tenant, conv, pairs=2)  # 4 messages
    memory = MemoryService(
        uow_factory, MockModelGateway(), recent_turns=2, summary_trigger_turns=4
    )
    await memory.maybe_summarize(tenant, conv)
    async with uow_factory() as uow:
        assert await uow.summaries.latest(tenant, conv) is None
