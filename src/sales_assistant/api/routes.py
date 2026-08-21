from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Annotated, Any, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field, field_validator

from sales_assistant.api.auth import Authenticator
from sales_assistant.application.conversation_service import (
    ConversationService,
    SendMessageResult,
)
from sales_assistant.domain import (
    AgentRun,
    AuthContext,
    Conversation,
    DomainError,
    LeaseManager,
    Message,
    RunEventStream,
    StoredRunEvent,
)
from sales_assistant.infrastructure.mysql.database import Database

_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


async def drain_background_tasks(timeout_seconds: float) -> None:
    if not _BACKGROUND_TASKS:
        return
    _, pending = await asyncio.wait(_BACKGROUND_TASKS, timeout=timeout_seconds)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=8_000)
    stream: bool = False

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("content cannot be blank")
        return normalized


class ConversationResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    owner_id: UUID
    title: str | None
    status: str
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, conversation: Conversation) -> ConversationResponse:
        return cls(
            id=conversation.id,
            tenant_id=conversation.tenant_id,
            owner_id=conversation.owner_id,
            title=conversation.title,
            status=conversation.status.value,
            version=conversation.version,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )


class MessageResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    role: str
    content: str
    sequence: int
    citations: list[dict[str, Any]]
    created_at: datetime

    @classmethod
    def from_domain(cls, message: Message) -> MessageResponse:
        return cls(
            id=message.id,
            conversation_id=message.conversation_id,
            role=message.role.value,
            content=message.content,
            sequence=message.sequence,
            citations=message.citations,
            created_at=message.created_at,
        )


class RunResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    status: str
    user_message_id: UUID | None
    assistant_message_id: UUID | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, run: AgentRun) -> RunResponse:
        return cls(
            id=run.id,
            conversation_id=run.conversation_id,
            status=run.status.value,
            user_message_id=run.user_message_id,
            assistant_message_id=run.assistant_message_id,
            error_code=run.error_code,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )


class SendMessageResponse(BaseModel):
    run: RunResponse
    user_message: MessageResponse
    assistant_message: MessageResponse | None
    replayed: bool

    @classmethod
    def from_result(cls, result: SendMessageResult) -> SendMessageResponse:
        return cls(
            run=RunResponse.from_domain(result.run),
            user_message=MessageResponse.from_domain(result.user_message),
            assistant_message=(
                MessageResponse.from_domain(result.assistant_message)
                if result.assistant_message is not None
                else None
            ),
            replayed=result.replayed,
        )


class MessageListResponse(BaseModel):
    items: list[MessageResponse]
    next_before_sequence: int | None


class _ApiContainer(Protocol):
    authenticator: Authenticator
    conversation_service: ConversationService
    database: Database
    lease_manager: LeaseManager
    event_stream: RunEventStream


def _container(request: Request) -> _ApiContainer:
    return cast(_ApiContainer, request.app.state.container)


async def get_auth_context(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_tenant_id: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[str | None, Header()] = None,
) -> AuthContext:
    return await _container(request).authenticator.authenticate(
        authorization=authorization,
        tenant_header=x_tenant_id,
        user_header=x_user_id,
    )


def get_conversation_service(request: Request) -> ConversationService:
    return _container(request).conversation_service


Auth = Annotated[AuthContext, Depends(get_auth_context)]

router = APIRouter()


@router.get("/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", tags=["health"])
async def readiness(request: Request) -> dict[str, str]:
    await _container(request).database.health_check()
    await _container(request).lease_manager.health_check()
    await _container(request).event_stream.health_check()
    return {"status": "ready"}


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.post(
    "/v1/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["conversations"],
)
async def create_conversation(
    payload: CreateConversationRequest,
    context: Auth,
    request: Request,
) -> ConversationResponse:
    conversation = await get_conversation_service(request).create_conversation(
        context,
        title=payload.title,
    )
    return ConversationResponse.from_domain(conversation)


@router.get(
    "/v1/conversations/{conversation_id}",
    response_model=ConversationResponse,
    tags=["conversations"],
)
async def get_conversation(
    conversation_id: UUID,
    context: Auth,
    request: Request,
) -> ConversationResponse:
    conversation = await get_conversation_service(request).get_conversation(
        context,
        conversation_id,
    )
    return ConversationResponse.from_domain(conversation)


@router.get(
    "/v1/conversations/{conversation_id}/messages",
    response_model=MessageListResponse,
    tags=["conversations"],
)
async def list_messages(
    conversation_id: UUID,
    context: Auth,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before_sequence: Annotated[int | None, Query(ge=1)] = None,
) -> MessageListResponse:
    messages = await get_conversation_service(request).list_messages(
        context,
        conversation_id,
        limit=limit,
        before_sequence=before_sequence,
    )
    next_before = messages[0].sequence if len(messages) == limit else None
    return MessageListResponse(
        items=[MessageResponse.from_domain(message) for message in messages],
        next_before_sequence=next_before,
    )


@router.post(
    "/v1/conversations/{conversation_id}/messages",
    response_model=None,
    tags=["conversations"],
)
async def send_message(
    conversation_id: UUID,
    payload: SendMessageRequest,
    context: Auth,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> SendMessageResponse | StreamingResponse:
    if payload.stream:
        return StreamingResponse(
            _stream_message(
                get_conversation_service(request),
                context,
                conversation_id,
                content=payload.content,
                idempotency_key=idempotency_key,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    result = await get_conversation_service(request).send_message(
        context,
        conversation_id,
        content=payload.content,
        idempotency_key=idempotency_key,
    )
    return SendMessageResponse.from_result(result)


def _encode_sse(event: StoredRunEvent) -> str:
    data = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.id}\nevent: {event.event_type}\ndata: {data}\n\n"


async def _stream_message(
    service: ConversationService,
    context: AuthContext,
    conversation_id: UUID,
    *,
    content: str,
    idempotency_key: str,
) -> AsyncGenerator[str]:
    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=256)

    async def on_event(event: StoredRunEvent) -> None:
        await queue.put(_encode_sse(event))

    async def produce() -> None:
        try:
            await service.send_message(
                context,
                conversation_id,
                content=content,
                idempotency_key=idempotency_key,
                on_event=on_event,
            )
        except Exception as error:
            code = getattr(error, "code", "INTERNAL_ERROR")
            message = str(error) if isinstance(error, DomainError) else "internal stream error"
            data = json.dumps(
                {"code": code, "message": message},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            await queue.put(f"event: stream.error\ndata: {data}\n\n")
        finally:
            await queue.put(None)

    task = asyncio.create_task(produce(), name=f"stream-message-{conversation_id}")
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=15)
        except TimeoutError:
            yield ": keep-alive\n\n"
            continue
        if item is None:
            return
        yield item


@router.get("/v1/runs/{run_id}", response_model=RunResponse, tags=["runs"])
async def get_run(
    run_id: UUID,
    context: Auth,
    request: Request,
) -> RunResponse:
    run = await get_conversation_service(request).get_run(context, run_id)
    return RunResponse.from_domain(run)


@router.get("/v1/runs/{run_id}/events", tags=["runs"])
async def resume_run_events(
    run_id: UUID,
    context: Auth,
    request: Request,
    last_event_id: Annotated[
        str,
        Header(alias="Last-Event-ID", pattern=r"^\d+-\d+$"),
    ] = "0-0",
) -> StreamingResponse:
    service = get_conversation_service(request)
    await service.get_run(context, run_id)
    event_stream = _container(request).event_stream

    async def generate() -> AsyncGenerator[str]:
        cursor = last_event_id
        terminal_events = {
            "message.completed",
            "run.failed",
            "run.conflicted",
            "run.cancelled",
            "run.replayed",
        }
        while True:
            events = await event_stream.read(
                run_id,
                after_id=cursor,
                block_milliseconds=15_000,
            )
            if not events:
                run = await service.get_run(context, run_id)
                if run.status.value in {"succeeded", "failed", "cancelled", "conflicted"}:
                    return
                yield ": keep-alive\n\n"
                continue
            for event in events:
                cursor = event.id
                yield _encode_sse(event)
                if event.event_type in terminal_events:
                    return

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/v1/runs/{run_id}/cancel", response_model=RunResponse, tags=["runs"])
async def cancel_run(
    run_id: UUID,
    context: Auth,
    request: Request,
) -> RunResponse:
    run = await get_conversation_service(request).cancel_run(context, run_id)
    return RunResponse.from_domain(run)
