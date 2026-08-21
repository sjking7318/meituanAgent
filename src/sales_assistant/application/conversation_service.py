from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

import structlog

from sales_assistant.application.memory_service import MemoryService
from sales_assistant.domain import (
    AgentRun,
    AuthContext,
    ConcurrentWriteError,
    Conversation,
    DependencyUnavailableError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    LeaseManager,
    Message,
    MessageRole,
    ModelTurn,
    ResourceNotFoundError,
    RunEventStream,
    RunStatus,
    StoredRunEvent,
    UnitOfWork,
    UnitOfWorkFactory,
    utc_now,
)

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class AgentOutcome:
    answer: str
    model: str
    input_tokens: int
    output_tokens: int
    citations: list[dict[str, Any]] = field(default_factory=list)


class AgentExecutor(Protocol):
    """Application-facing view of the agent runtime (framework-agnostic)."""

    async def run(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
        conversation_id: UUID,
        user_query: str,
        history: Sequence[ModelTurn] = (),
    ) -> AgentOutcome: ...


@dataclass(frozen=True, slots=True)
class SendMessageResult:
    run: AgentRun
    user_message: Message
    assistant_message: Message | None
    replayed: bool


RunEventCallback = Callable[[StoredRunEvent], Awaitable[None]]


class ConversationService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        agent_executor: AgentExecutor,
        lease_manager: LeaseManager,
        event_stream: RunEventStream,
        memory_service: MemoryService | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._agent_executor = agent_executor
        self._lease_manager = lease_manager
        self._event_stream = event_stream
        self._memory_service = memory_service

    async def create_conversation(
        self,
        context: AuthContext,
        *,
        title: str | None,
    ) -> Conversation:
        conversation = Conversation(
            tenant_id=context.tenant_id,
            owner_id=context.user_id,
            title=title,
        )
        async with self._unit_of_work_factory() as unit_of_work:
            await unit_of_work.conversations.add(conversation)
            await unit_of_work.commit()
        return conversation

    async def get_conversation(
        self,
        context: AuthContext,
        conversation_id: UUID,
    ) -> Conversation:
        async with self._unit_of_work_factory() as unit_of_work:
            conversation = await unit_of_work.conversations.get(
                context.tenant_id,
                conversation_id,
            )
            if conversation is None:
                raise ResourceNotFoundError("conversation not found")
            conversation.assert_access(context)
            return conversation

    async def list_messages(
        self,
        context: AuthContext,
        conversation_id: UUID,
        *,
        limit: int,
        before_sequence: int | None,
    ) -> list[Message]:
        async with self._unit_of_work_factory() as unit_of_work:
            conversation = await unit_of_work.conversations.get(
                context.tenant_id,
                conversation_id,
            )
            if conversation is None:
                raise ResourceNotFoundError("conversation not found")
            conversation.assert_access(context)
            return await unit_of_work.messages.list(
                context.tenant_id,
                conversation_id,
                limit=limit,
                before_sequence=before_sequence,
            )

    async def send_message(
        self,
        context: AuthContext,
        conversation_id: UUID,
        *,
        content: str,
        idempotency_key: str,
        on_event: RunEventCallback | None = None,
    ) -> SendMessageResult:
        fingerprint = self._fingerprint(conversation_id, content)
        async with self._lease_manager.hold(context.tenant_id, conversation_id) as lease:
            try:
                prepared = await self._prepare_run(
                    context,
                    conversation_id,
                    content=content,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    fencing_token=lease.fencing_token,
                )
            except IdempotencyConflictError:
                return await self._load_idempotent_result(
                    context,
                    idempotency_key=idempotency_key,
                    expected_fingerprint=fingerprint,
                )

            if prepared.replayed:
                await self._emit(
                    prepared.run.id,
                    "run.replayed",
                    self._result_event_data(prepared),
                    on_event,
                )
                return prepared

            await self._emit(
                prepared.run.id,
                "run.started",
                {
                    "run_id": str(prepared.run.id),
                    "conversation_id": str(conversation_id),
                    "status": prepared.run.status.value,
                },
                on_event,
            )
            history = await self._load_history(
                context,
                conversation_id,
                before_sequence=prepared.user_message.sequence,
            )
            try:
                outcome = await self._agent_executor.run(
                    run_id=prepared.run.id,
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                    conversation_id=conversation_id,
                    user_query=content,
                    history=history,
                )
            except Exception as error:
                await self._mark_run_failed(context, prepared.run.id)
                await self._emit(
                    prepared.run.id,
                    "run.failed",
                    {
                        "run_id": str(prepared.run.id),
                        "code": "MODEL_UNAVAILABLE",
                        "message": "model service is unavailable",
                    },
                    on_event,
                )
                raise DependencyUnavailableError("model service is unavailable") from error

            try:
                await lease.ensure_valid()
            except ConcurrentWriteError:
                await self._mark_run_conflicted(context, prepared.run.id)
                await self._emit(
                    prepared.run.id,
                    "run.conflicted",
                    {"run_id": str(prepared.run.id), "code": "LEASE_LOST"},
                    on_event,
                )
                raise

            assistant_message = Message(
                tenant_id=context.tenant_id,
                conversation_id=conversation_id,
                role=MessageRole.ASSISTANT,
                content=outcome.answer,
                token_count=outcome.output_tokens,
                citations=outcome.citations,
                sequence=(prepared.run.expected_conversation_version + 1) * 2,
            )
            try:
                async with self._unit_of_work_factory() as unit_of_work:
                    await unit_of_work.conversations.lock_at_version(
                        context.tenant_id,
                        conversation_id,
                        prepared.run.expected_conversation_version + 1,
                    )
                    run = await unit_of_work.runs.get(context.tenant_id, prepared.run.id)
                    if run is None:
                        raise ResourceNotFoundError("run not found")
                    if run.status is not RunStatus.RUNNING:
                        raise InvalidStateTransitionError(
                            f"run is no longer running: {run.status.value}"
                        )
                    await unit_of_work.messages.add(assistant_message)
                    run.assistant_message_id = assistant_message.id
                    run.transition_to(RunStatus.SUCCEEDED)
                    await unit_of_work.runs.save(run)
                    await unit_of_work.commit()
            except ConcurrentWriteError:
                await self._mark_run_conflicted(context, prepared.run.id)
                await self._emit(
                    prepared.run.id,
                    "run.conflicted",
                    {"run_id": str(prepared.run.id), "code": "STALE_FENCING_VERSION"},
                    on_event,
                )
                raise

            result = SendMessageResult(
                run=run,
                user_message=prepared.user_message,
                assistant_message=assistant_message,
                replayed=False,
            )
            await self._emit(
                run.id,
                "message.completed",
                self._result_event_data(result),
                on_event,
            )
            if self._memory_service is not None:
                await self._memory_service.maybe_summarize(
                    context.tenant_id,
                    conversation_id,
                )
            return result

    async def get_run(self, context: AuthContext, run_id: UUID) -> AgentRun:
        async with self._unit_of_work_factory() as unit_of_work:
            run = await unit_of_work.runs.get(context.tenant_id, run_id)
            if run is None:
                raise ResourceNotFoundError("run not found")
            conversation = await unit_of_work.conversations.get(
                context.tenant_id,
                run.conversation_id,
            )
            if conversation is None:
                raise ResourceNotFoundError("conversation not found")
            conversation.assert_access(context)
            return run

    async def cancel_run(self, context: AuthContext, run_id: UUID) -> AgentRun:
        async with self._unit_of_work_factory() as unit_of_work:
            run = await unit_of_work.runs.get(context.tenant_id, run_id)
            if run is None:
                raise ResourceNotFoundError("run not found")
            conversation = await unit_of_work.conversations.get(
                context.tenant_id,
                run.conversation_id,
            )
            if conversation is None:
                raise ResourceNotFoundError("conversation not found")
            conversation.assert_access(context)
            run.transition_to(RunStatus.CANCELLED)
            await unit_of_work.runs.save(run)
            await unit_of_work.commit()
        await self._emit(
            run.id,
            "run.cancelled",
            {"run_id": str(run.id), "status": run.status.value},
            None,
        )
        return run

    async def _prepare_run(
        self,
        context: AuthContext,
        conversation_id: UUID,
        *,
        content: str,
        idempotency_key: str,
        fingerprint: str,
        fencing_token: int,
    ) -> SendMessageResult:
        async with self._unit_of_work_factory() as unit_of_work:
            existing = await unit_of_work.runs.get_by_idempotency_key(
                context.tenant_id,
                idempotency_key,
            )
            if existing is not None:
                return await self._build_existing_result(
                    unit_of_work,
                    context,
                    existing,
                    fingerprint,
                )

            conversation = await unit_of_work.conversations.get(
                context.tenant_id,
                conversation_id,
            )
            if conversation is None:
                raise ResourceNotFoundError("conversation not found")
            conversation.assert_access(context)

            old_version = conversation.version
            new_version = await unit_of_work.conversations.bump_version(
                context.tenant_id,
                conversation_id,
                old_version,
            )
            user_message = Message(
                tenant_id=context.tenant_id,
                conversation_id=conversation_id,
                role=MessageRole.USER,
                content=content,
                sequence=(new_version * 2) - 1,
            )
            run = AgentRun(
                tenant_id=context.tenant_id,
                conversation_id=conversation_id,
                user_id=context.user_id,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                expected_conversation_version=old_version,
                fencing_token=fencing_token,
                user_message_id=user_message.id,
            )
            run.transition_to(RunStatus.RUNNING)
            await unit_of_work.messages.add(user_message)
            await unit_of_work.runs.add(run)
            await unit_of_work.commit()
            return SendMessageResult(
                run=run,
                user_message=user_message,
                assistant_message=None,
                replayed=False,
            )

    async def _load_idempotent_result(
        self,
        context: AuthContext,
        *,
        idempotency_key: str,
        expected_fingerprint: str,
    ) -> SendMessageResult:
        async with self._unit_of_work_factory() as unit_of_work:
            run = await unit_of_work.runs.get_by_idempotency_key(
                context.tenant_id,
                idempotency_key,
            )
            if run is None:
                raise ConcurrentWriteError("idempotent request is not visible yet")
            return await self._build_existing_result(
                unit_of_work,
                context,
                run,
                expected_fingerprint,
            )

    @staticmethod
    async def _build_existing_result(
        unit_of_work: UnitOfWork,
        context: AuthContext,
        run: AgentRun,
        expected_fingerprint: str,
    ) -> SendMessageResult:
        if run.request_fingerprint != expected_fingerprint:
            raise IdempotencyConflictError("idempotency key was used with a different request")

        assert run.user_message_id is not None
        user_message = await unit_of_work.messages.get(context.tenant_id, run.user_message_id)
        if user_message is None:
            raise ResourceNotFoundError("idempotent user message not found")
        assistant_message = None
        if run.assistant_message_id is not None:
            assistant_message = await unit_of_work.messages.get(
                context.tenant_id,
                run.assistant_message_id,
            )
        return SendMessageResult(
            run=run,
            user_message=user_message,
            assistant_message=assistant_message,
            replayed=True,
        )

    async def _mark_run_failed(self, context: AuthContext, run_id: UUID) -> None:
        async with self._unit_of_work_factory() as unit_of_work:
            run = await unit_of_work.runs.get(context.tenant_id, run_id)
            if run is None or run.status is not RunStatus.RUNNING:
                return
            run.transition_to(RunStatus.FAILED, error_code="MODEL_UNAVAILABLE")
            await unit_of_work.runs.save(run)
            await unit_of_work.commit()

    async def _mark_run_conflicted(self, context: AuthContext, run_id: UUID) -> None:
        async with self._unit_of_work_factory() as unit_of_work:
            run = await unit_of_work.runs.get(context.tenant_id, run_id)
            if run is None or run.status is not RunStatus.RUNNING:
                return
            run.transition_to(RunStatus.CONFLICTED, error_code="LEASE_LOST")
            await unit_of_work.runs.save(run)
            await unit_of_work.commit()

    async def _load_history(
        self,
        context: AuthContext,
        conversation_id: UUID,
        *,
        before_sequence: int,
    ) -> tuple[ModelTurn, ...]:
        # Memory-managed path: rolling summary + sliding window (memory-design.md 2/3).
        if self._memory_service is not None:
            return await self._memory_service.load_context(
                context.tenant_id,
                conversation_id,
                before_sequence=before_sequence,
            )
        async with self._unit_of_work_factory() as unit_of_work:
            messages = await unit_of_work.messages.list(
                context.tenant_id,
                conversation_id,
                limit=16,
                before_sequence=before_sequence,
            )
        allowed_roles = {MessageRole.USER, MessageRole.ASSISTANT}
        return tuple(
            ModelTurn(role=message.role, content=message.content)
            for message in messages
            if message.role in allowed_roles
        )

    async def _emit(
        self,
        run_id: UUID,
        event_type: str,
        data: dict[str, Any],
        callback: RunEventCallback | None,
    ) -> None:
        try:
            event = await self._event_stream.append(run_id, event_type, data)
        except Exception:
            logger.exception(
                "run_event_persistence_failed",
                run_id=str(run_id),
                event_type=event_type,
            )
            event = StoredRunEvent(
                id="0-0",
                run_id=run_id,
                event_type=event_type,
                data=data,
                created_at=utc_now(),
            )
        if callback is not None:
            try:
                await callback(event)
            except Exception:
                logger.exception(
                    "run_event_callback_failed",
                    run_id=str(run_id),
                    event_type=event_type,
                )

    @staticmethod
    def _result_event_data(result: SendMessageResult) -> dict[str, Any]:
        assistant = result.assistant_message
        return {
            "run_id": str(result.run.id),
            "conversation_id": str(result.run.conversation_id),
            "status": result.run.status.value,
            "replayed": result.replayed,
            "user_message_id": str(result.user_message.id),
            "assistant_message": (
                {
                    "id": str(assistant.id),
                    "role": assistant.role.value,
                    "content": assistant.content,
                    "sequence": assistant.sequence,
                    "created_at": assistant.created_at.isoformat(),
                }
                if assistant is not None
                else None
            ),
        }

    @staticmethod
    def _fingerprint(conversation_id: UUID, content: str) -> str:
        normalized = content.strip().encode()
        return hashlib.sha256(conversation_id.bytes + b"\0" + normalized).hexdigest()
