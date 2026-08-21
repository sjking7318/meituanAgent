"""Contract tests for the MySQL LangGraph checkpointer (T-107).

Requires a running MySQL (``make up`` + ``make migrate``). Skipped automatically
if the database is unreachable, so ``make test`` stays hermetic.

Run explicitly with: ``uv run pytest tests/integration -m integration``
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata, empty_checkpoint

from sales_assistant.agents.runtime.checkpointer import MySQLCheckpointSaver
from sales_assistant.infrastructure.mysql.database import Database

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "mysql+asyncmy://sales:sales@localhost:3306/sales_assistant",
)


@pytest_asyncio.fixture
async def saver() -> AsyncIterator[MySQLCheckpointSaver]:
    database = Database(_DB_URL)
    try:
        await database.health_check()
    except Exception:
        await database.dispose()
        pytest.skip("MySQL not reachable; run `make up && make migrate`")
    yield MySQLCheckpointSaver(database.session_factory)
    await database.dispose()


def _config(thread_id: str, tenant_id: uuid.UUID) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "tenant_id": tenant_id}}


async def test_put_and_get_roundtrip(saver: MySQLCheckpointSaver) -> None:
    thread_id = f"test-{uuid.uuid4()}"
    tenant_id = uuid.uuid4()
    config = _config(thread_id, tenant_id)
    checkpoint: Checkpoint = empty_checkpoint()
    metadata: CheckpointMetadata = {"source": "loop", "step": 1}

    saved_config = await saver.aput(config, checkpoint, metadata, {})
    fetched = await saver.aget_tuple(saved_config)

    assert fetched is not None
    assert fetched.checkpoint["id"] == checkpoint["id"]
    assert fetched.metadata["step"] == 1
    await saver.adelete_thread(thread_id)


async def test_cross_instance_resume(saver: MySQLCheckpointSaver) -> None:
    """A second saver instance (fresh sessions) must see persisted state."""
    thread_id = f"test-{uuid.uuid4()}"
    tenant_id = uuid.uuid4()
    config = _config(thread_id, tenant_id)
    checkpoint: Checkpoint = empty_checkpoint()
    await saver.aput(config, checkpoint, {"source": "input", "step": 0}, {})

    # Simulate a different instance by using a new saver over the same DB.
    database = Database(_DB_URL)
    other = MySQLCheckpointSaver(database.session_factory)
    try:
        fetched = await other.aget_tuple(config)
        assert fetched is not None
        assert fetched.checkpoint["id"] == checkpoint["id"]
    finally:
        await database.dispose()
    await saver.adelete_thread(thread_id)


async def test_put_writes_and_pending(saver: MySQLCheckpointSaver) -> None:
    thread_id = f"test-{uuid.uuid4()}"
    tenant_id = uuid.uuid4()
    config = _config(thread_id, tenant_id)
    checkpoint: Checkpoint = empty_checkpoint()
    saved = await saver.aput(config, checkpoint, {"source": "loop", "step": 1}, {})

    await saver.aput_writes(saved, [("messages", {"role": "assistant"})], task_id="task-1")
    fetched = await saver.aget_tuple(saved)

    assert fetched is not None
    assert fetched.pending_writes is not None
    channels = [channel for _, channel, _ in fetched.pending_writes]
    assert "messages" in channels
    await saver.adelete_thread(thread_id)


async def test_list_returns_checkpoints(saver: MySQLCheckpointSaver) -> None:
    thread_id = f"test-{uuid.uuid4()}"
    tenant_id = uuid.uuid4()
    config = _config(thread_id, tenant_id)
    await saver.aput(config, empty_checkpoint(), {"source": "input", "step": 0}, {})
    await saver.aput(config, empty_checkpoint(), {"source": "loop", "step": 1}, {})

    found = [tuple_ async for tuple_ in saver.alist(config)]
    assert len(found) >= 2
    await saver.adelete_thread(thread_id)
