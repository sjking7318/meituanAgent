from __future__ import annotations

from types import TracebackType
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Select, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sales_assistant.domain import (
    AgentRun,
    ConcurrentWriteError,
    Conversation,
    ConversationRepository,
    ConversationStatus,
    ConversationSummary,
    ConversationSummaryRepository,
    IdempotencyConflictError,
    Message,
    MessageRepository,
    MessageRole,
    RunRepository,
    RunStatus,
    UnitOfWork,
    utc_now,
)
from sales_assistant.infrastructure.mysql.models import (
    AgentRunRecord,
    ConversationRecord,
    ConversationSummaryRecord,
    MessageRecord,
)


def _to_conversation(record: ConversationRecord) -> Conversation:
    return Conversation(
        id=record.id,
        tenant_id=record.tenant_id,
        owner_id=record.owner_id,
        title=record.title,
        status=ConversationStatus(record.status),
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _to_message(record: MessageRecord) -> Message:
    return Message(
        id=record.id,
        tenant_id=record.tenant_id,
        conversation_id=record.conversation_id,
        role=MessageRole(record.role),
        content=record.content,
        token_count=record.token_count,
        citations=list(record.citations or []),
        sequence=record.sequence,
        created_at=record.created_at,
    )


def _to_summary(record: ConversationSummaryRecord) -> ConversationSummary:
    return ConversationSummary(
        id=record.id,
        tenant_id=record.tenant_id,
        conversation_id=record.conversation_id,
        summary=record.summary,
        covered_through_sequence=record.covered_through_sequence,
        source_version=record.source_version,
        created_at=record.created_at,
    )


def _to_run(record: AgentRunRecord) -> AgentRun:
    return AgentRun(
        id=record.id,
        tenant_id=record.tenant_id,
        conversation_id=record.conversation_id,
        user_id=record.user_id,
        idempotency_key=record.idempotency_key,
        request_fingerprint=record.request_fingerprint,
        expected_conversation_version=record.expected_conversation_version,
        fencing_token=record.fencing_token,
        status=RunStatus(record.status),
        user_message_id=record.user_message_id,
        assistant_message_id=record.assistant_message_id,
        error_code=record.error_code,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


class SqlConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, conversation: Conversation) -> None:
        self._session.add(
            ConversationRecord(
                id=conversation.id,
                tenant_id=conversation.tenant_id,
                owner_id=conversation.owner_id,
                title=conversation.title,
                status=conversation.status.value,
                version=conversation.version,
                created_at=conversation.created_at,
                updated_at=conversation.updated_at,
            )
        )
        await self._session.flush()

    async def get(self, tenant_id: UUID, conversation_id: UUID) -> Conversation | None:
        statement = select(ConversationRecord).where(
            ConversationRecord.id == conversation_id,
            ConversationRecord.tenant_id == tenant_id,
        )
        record = await self._session.scalar(statement)
        return _to_conversation(record) if record is not None else None

    async def bump_version(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
        expected_version: int,
    ) -> int:
        new_version = expected_version + 1
        statement = (
            update(ConversationRecord)
            .where(
                ConversationRecord.id == conversation_id,
                ConversationRecord.tenant_id == tenant_id,
                ConversationRecord.version == expected_version,
            )
            .values(version=new_version, updated_at=utc_now())
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        if result.rowcount != 1:
            raise ConcurrentWriteError("conversation was modified by another request")
        return new_version

    async def lock_at_version(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
        expected_version: int,
    ) -> None:
        statement = (
            select(ConversationRecord.version)
            .where(
                ConversationRecord.id == conversation_id,
                ConversationRecord.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        current_version = await self._session.scalar(statement)
        if current_version != expected_version:
            raise ConcurrentWriteError("run fencing version is stale")


class SqlMessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, message: Message) -> None:
        self._session.add(
            MessageRecord(
                id=message.id,
                tenant_id=message.tenant_id,
                conversation_id=message.conversation_id,
                role=message.role.value,
                content=message.content,
                token_count=message.token_count,
                citations=list(message.citations),
                sequence=message.sequence,
                created_at=message.created_at,
            )
        )
        await self._session.flush()

    async def list(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
        *,
        limit: int,
        before_sequence: int | None = None,
    ) -> list[Message]:
        statement: Select[tuple[MessageRecord]] = select(MessageRecord).where(
            MessageRecord.tenant_id == tenant_id,
            MessageRecord.conversation_id == conversation_id,
        )
        if before_sequence is not None:
            statement = statement.where(MessageRecord.sequence < before_sequence)
        statement = statement.order_by(MessageRecord.sequence.desc()).limit(limit)
        records = list((await self._session.scalars(statement)).all())
        return [_to_message(record) for record in reversed(records)]

    async def get(self, tenant_id: UUID, message_id: UUID) -> Message | None:
        statement = select(MessageRecord).where(
            MessageRecord.id == message_id,
            MessageRecord.tenant_id == tenant_id,
        )
        record = await self._session.scalar(statement)
        return _to_message(record) if record is not None else None


class SqlConversationSummaryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, summary: ConversationSummary) -> None:
        self._session.add(
            ConversationSummaryRecord(
                id=summary.id,
                tenant_id=summary.tenant_id,
                conversation_id=summary.conversation_id,
                summary=summary.summary,
                covered_through_sequence=summary.covered_through_sequence,
                source_version=summary.source_version,
                created_at=summary.created_at,
            )
        )
        await self._session.flush()

    async def latest(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
    ) -> ConversationSummary | None:
        statement = (
            select(ConversationSummaryRecord)
            .where(
                ConversationSummaryRecord.tenant_id == tenant_id,
                ConversationSummaryRecord.conversation_id == conversation_id,
            )
            .order_by(ConversationSummaryRecord.source_version.desc())
            .limit(1)
        )
        record = await self._session.scalar(statement)
        return _to_summary(record) if record is not None else None


class SqlRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, run: AgentRun) -> None:
        self._session.add(
            AgentRunRecord(
                id=run.id,
                tenant_id=run.tenant_id,
                conversation_id=run.conversation_id,
                user_id=run.user_id,
                idempotency_key=run.idempotency_key,
                request_fingerprint=run.request_fingerprint,
                expected_conversation_version=run.expected_conversation_version,
                fencing_token=run.fencing_token,
                status=run.status.value,
                user_message_id=run.user_message_id,
                assistant_message_id=run.assistant_message_id,
                error_code=run.error_code,
                created_at=run.created_at,
                updated_at=run.updated_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise IdempotencyConflictError("idempotency key already exists") from error

    async def get(self, tenant_id: UUID, run_id: UUID) -> AgentRun | None:
        statement = select(AgentRunRecord).where(
            AgentRunRecord.id == run_id,
            AgentRunRecord.tenant_id == tenant_id,
        )
        record = await self._session.scalar(statement)
        return _to_run(record) if record is not None else None

    async def get_by_idempotency_key(
        self,
        tenant_id: UUID,
        idempotency_key: str,
    ) -> AgentRun | None:
        statement = select(AgentRunRecord).where(
            AgentRunRecord.tenant_id == tenant_id,
            AgentRunRecord.idempotency_key == idempotency_key,
        )
        record = await self._session.scalar(statement)
        return _to_run(record) if record is not None else None

    async def save(self, run: AgentRun) -> None:
        statement = (
            update(AgentRunRecord)
            .where(
                AgentRunRecord.id == run.id,
                AgentRunRecord.tenant_id == run.tenant_id,
            )
            .values(
                status=run.status.value,
                fencing_token=run.fencing_token,
                user_message_id=run.user_message_id,
                assistant_message_id=run.assistant_message_id,
                error_code=run.error_code,
                updated_at=run.updated_at,
            )
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        if result.rowcount != 1:
            raise ConcurrentWriteError("run was modified or removed")


class SqlUnitOfWork:
    conversations: ConversationRepository
    messages: MessageRepository
    summaries: ConversationSummaryRepository
    runs: RunRepository

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._committed = False

    async def __aenter__(self) -> SqlUnitOfWork:
        self._session = self._session_factory()
        self.conversations = SqlConversationRepository(self._session)
        self.messages = SqlMessageRepository(self._session)
        self.summaries = SqlConversationSummaryRepository(self._session)
        self.runs = SqlRunRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        if exc is not None or not self._committed:
            await self._session.rollback()
        await self._session.close()

    async def commit(self) -> None:
        if self._session is None:
            raise RuntimeError("unit of work has not been entered")
        await self._session.commit()
        self._committed = True

    async def rollback(self) -> None:
        if self._session is None:
            raise RuntimeError("unit of work has not been entered")
        await self._session.rollback()


class SqlUnitOfWorkFactory:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def __call__(self) -> UnitOfWork:
        return SqlUnitOfWork(self._session_factory)
