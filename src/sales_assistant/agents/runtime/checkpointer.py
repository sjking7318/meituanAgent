from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, Self

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy import delete, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sales_assistant.infrastructure.mysql.models import (
    LangGraphCheckpointRecord,
    LangGraphCheckpointWriteRecord,
)

# Sentinel tenant used when a graph runs without a bound tenant (should not
# happen in production paths, but keeps the checkpointer usable in isolation).
_NULL_TENANT = uuid.UUID(int=0)


def _now() -> datetime:
    return datetime.now(UTC)


def _tenant_from_config(config: RunnableConfig) -> uuid.UUID:
    raw = config.get("configurable", {}).get("tenant_id")
    if raw is None:
        return _NULL_TENANT
    return raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))


def _checkpoint_config(thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint_id,
        }
    }


class MySQLCheckpointSaver(BaseCheckpointSaver[str]):
    """Async LangGraph checkpointer backed by MySQL (ADR-0004).

    Persists checkpoints and pending channel writes so any Agent Runtime
    instance can resume a run by ``thread_id`` (= run_id).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(serde=JsonPlusSerializer())
        self._session_factory = session_factory

    # ------------------------------------------------------------------ async

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = configurable.get("checkpoint_ns", "")
        checkpoint_id = checkpoint["id"]
        parent_id = configurable.get("checkpoint_id")

        ckpt_type, ckpt_blob = self.serde.dumps_typed(checkpoint)
        meta_type, meta_blob = self.serde.dumps_typed(dict(metadata))
        # checkpoint and metadata share the same serde type tag.
        _ = meta_type

        async with self._session_factory() as session:
            stmt = mysql_insert(LangGraphCheckpointRecord).values(
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                checkpoint_id=checkpoint_id,
                tenant_id=_tenant_from_config(config),
                parent_checkpoint_id=parent_id,
                checkpoint_type=ckpt_type,
                checkpoint_blob=ckpt_blob,
                metadata_blob=meta_blob,
                created_at=_now(),
            )
            stmt = stmt.on_duplicate_key_update(
                checkpoint_type=stmt.inserted.checkpoint_type,
                checkpoint_blob=stmt.inserted.checkpoint_blob,
                metadata_blob=stmt.inserted.metadata_blob,
                parent_checkpoint_id=stmt.inserted.parent_checkpoint_id,
            )
            await session.execute(stmt)
            await session.commit()

        return _checkpoint_config(thread_id, checkpoint_ns, checkpoint_id)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = configurable.get("checkpoint_ns", "")
        checkpoint_id = configurable["checkpoint_id"]
        tenant_id = _tenant_from_config(config)

        async with self._session_factory() as session:
            for offset, (channel, value) in enumerate(writes):
                write_type, write_blob = self.serde.dumps_typed(value)
                idx = WRITES_IDX_MAP.get(channel, offset)
                stmt = mysql_insert(LangGraphCheckpointWriteRecord).values(
                    thread_id=thread_id,
                    checkpoint_ns=checkpoint_ns,
                    checkpoint_id=checkpoint_id,
                    task_id=task_id,
                    idx=idx,
                    tenant_id=tenant_id,
                    channel=channel,
                    write_type=write_type,
                    write_blob=write_blob,
                    created_at=_now(),
                )
                stmt = stmt.on_duplicate_key_update(
                    channel=stmt.inserted.channel,
                    write_type=stmt.inserted.write_type,
                    write_blob=stmt.inserted.write_blob,
                )
                await session.execute(stmt)
            await session.commit()

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = configurable.get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)

        async with self._session_factory() as session:
            stmt = select(LangGraphCheckpointRecord).where(
                LangGraphCheckpointRecord.thread_id == thread_id,
                LangGraphCheckpointRecord.checkpoint_ns == checkpoint_ns,
            )
            if checkpoint_id:
                stmt = stmt.where(LangGraphCheckpointRecord.checkpoint_id == checkpoint_id)
            else:
                stmt = stmt.order_by(LangGraphCheckpointRecord.checkpoint_id.desc()).limit(1)

            record = await session.scalar(stmt)
            if record is None:
                return None

            pending_writes = await self._load_writes(
                session, thread_id, checkpoint_ns, record.checkpoint_id
            )

        checkpoint = self.serde.loads_typed((record.checkpoint_type, record.checkpoint_blob))
        metadata = self.serde.loads_typed((record.checkpoint_type, record.metadata_blob))
        parent_config = None
        if record.parent_checkpoint_id is not None:
            parent_config = _checkpoint_config(
                thread_id, checkpoint_ns, record.parent_checkpoint_id
            )
        return CheckpointTuple(
            config=_checkpoint_config(thread_id, checkpoint_ns, record.checkpoint_id),
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=pending_writes,
        )

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 - matches base signature
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            return
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = configurable.get("checkpoint_ns", "")

        async with self._session_factory() as session:
            stmt = (
                select(LangGraphCheckpointRecord)
                .where(
                    LangGraphCheckpointRecord.thread_id == thread_id,
                    LangGraphCheckpointRecord.checkpoint_ns == checkpoint_ns,
                )
                .order_by(LangGraphCheckpointRecord.checkpoint_id.desc())
            )
            if before is not None:
                before_id = before["configurable"]["checkpoint_id"]
                stmt = stmt.where(LangGraphCheckpointRecord.checkpoint_id < before_id)
            if limit is not None:
                stmt = stmt.limit(limit)

            records = list((await session.scalars(stmt)).all())
            for record in records:
                writes = await self._load_writes(
                    session, thread_id, checkpoint_ns, record.checkpoint_id
                )
                checkpoint = self.serde.loads_typed(
                    (record.checkpoint_type, record.checkpoint_blob)
                )
                metadata = self.serde.loads_typed((record.checkpoint_type, record.metadata_blob))
                parent_config = None
                if record.parent_checkpoint_id is not None:
                    parent_config = _checkpoint_config(
                        thread_id, checkpoint_ns, record.parent_checkpoint_id
                    )
                yield CheckpointTuple(
                    config=_checkpoint_config(thread_id, checkpoint_ns, record.checkpoint_id),
                    checkpoint=checkpoint,
                    metadata=metadata,
                    parent_config=parent_config,
                    pending_writes=writes,
                )

    async def adelete_thread(self, thread_id: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                delete(LangGraphCheckpointRecord).where(
                    LangGraphCheckpointRecord.thread_id == thread_id
                )
            )
            await session.execute(
                delete(LangGraphCheckpointWriteRecord).where(
                    LangGraphCheckpointWriteRecord.thread_id == thread_id
                )
            )
            await session.commit()

    async def _load_writes(
        self,
        session: AsyncSession,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
    ) -> list[tuple[str, str, Any]]:
        stmt = (
            select(LangGraphCheckpointWriteRecord)
            .where(
                LangGraphCheckpointWriteRecord.thread_id == thread_id,
                LangGraphCheckpointWriteRecord.checkpoint_ns == checkpoint_ns,
                LangGraphCheckpointWriteRecord.checkpoint_id == checkpoint_id,
            )
            .order_by(
                LangGraphCheckpointWriteRecord.task_id,
                LangGraphCheckpointWriteRecord.idx,
            )
        )
        rows = list((await session.scalars(stmt)).all())
        return [
            (row.task_id, row.channel, self.serde.loads_typed((row.write_type, row.write_blob)))
            for row in rows
        ]

    # ------------------------------------------------------------------- sync

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        raise NotImplementedError("Use the async API (aput) with MySQLCheckpointSaver")

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        raise NotImplementedError("Use the async API (aput_writes) with MySQLCheckpointSaver")

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        raise NotImplementedError("Use the async API (aget_tuple) with MySQLCheckpointSaver")

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 - matches base signature
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        raise NotImplementedError("Use the async API (alist) with MySQLCheckpointSaver")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: Any) -> None:
        return None
