from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from uuid_extensions import uuid7

from sales_assistant.domain.errors import (
    InvalidStateTransitionError,
    ResourceForbiddenError,
    ResourceNotFoundError,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> UUID:
    """Time-ordered UUIDv7 (ADR-0001): index-friendly for MySQL BINARY(16)."""
    return UUID(str(uuid7()))


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CONFLICTED = "conflicted"


_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.CONFLICTED,
        }
    ),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.CONFLICTED: frozenset(),
}


@dataclass(slots=True)
class AuthContext:
    tenant_id: UUID
    user_id: UUID
    permissions: frozenset[str] = field(default_factory=frozenset)


@dataclass(slots=True)
class Conversation:
    tenant_id: UUID
    owner_id: UUID
    title: str | None = None
    id: UUID = field(default_factory=new_id)
    status: ConversationStatus = ConversationStatus.ACTIVE
    version: int = 0
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def assert_access(self, context: AuthContext) -> None:
        if self.tenant_id != context.tenant_id:
            raise ResourceNotFoundError("conversation not found")
        if self.owner_id != context.user_id and "conversation:read:any" not in context.permissions:
            raise ResourceForbiddenError("conversation is outside the caller's scope")


@dataclass(slots=True)
class Message:
    tenant_id: UUID
    conversation_id: UUID
    role: MessageRole
    content: str
    sequence: int
    token_count: int = 0
    citations: list[dict[str, Any]] = field(default_factory=list)
    id: UUID = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class AgentRun:
    tenant_id: UUID
    conversation_id: UUID
    user_id: UUID
    idempotency_key: str
    request_fingerprint: str
    expected_conversation_version: int
    id: UUID = field(default_factory=new_id)
    fencing_token: int | None = None
    status: RunStatus = RunStatus.CREATED
    user_message_id: UUID | None = None
    assistant_message_id: UUID | None = None
    error_code: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def transition_to(self, target: RunStatus, *, error_code: str | None = None) -> None:
        if target not in _RUN_TRANSITIONS[self.status]:
            raise InvalidStateTransitionError(
                f"cannot transition run from {self.status} to {target}"
            )
        self.status = target
        self.error_code = error_code
        self.updated_at = utc_now()


@dataclass(frozen=True, slots=True)
class ModelTurn:
    role: MessageRole
    content: str


@dataclass(frozen=True, slots=True)
class ModelRequest:
    system_prompt: str
    user_prompt: str
    conversation_id: UUID
    run_id: UUID
    history: tuple[ModelTurn, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str
    model: str
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """Versioned rolling summary of a conversation (memory-design.md 3, 5).

    Each summary covers messages up to ``covered_through_sequence`` and is
    immutable: advancing the summary appends a new version rather than
    rewriting, so instances never clobber a newer summary (source_version).
    """

    tenant_id: UUID
    conversation_id: UUID
    summary: str
    covered_through_sequence: int
    source_version: int
    id: UUID = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class StoredRunEvent:
    id: str
    run_id: UUID
    event_type: str
    data: dict[str, Any]
    created_at: datetime


class RecallSource(StrEnum):
    DENSE = "dense"
    BM25 = "bm25"


@dataclass(frozen=True, slots=True)
class RetrievalFilters:
    """Scalar filters applied before recall (ADR-0002, rag-design.md 3.2)."""

    knowledge_base_ids: tuple[UUID, ...] = ()
    products: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    max_security_level: str = "normal"


@dataclass(frozen=True, slots=True)
class Candidate:
    """A recalled child chunk with its parent context and provenance."""

    chunk_id: str
    document_version_id: str
    parent_id: str
    text: str
    score: float
    source: RecallSource
    title: str | None = None
    section_path: str | None = None
    page: int | None = None


@dataclass(frozen=True, slots=True)
class Evidence:
    """A packed, citable unit of evidence used for answer generation."""

    chunk_id: str
    document_version_id: str
    text: str
    score: float
    title: str | None = None
    section_path: str | None = None
    page: int | None = None


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """A child chunk ready to be indexed into the vector store."""

    chunk_id: str
    parent_id: str
    text: str
    dense: list[float]
    tenant_id: UUID
    knowledge_base_id: UUID
    document_version_id: UUID
    acl_tokens: tuple[str, ...]
    status: str
    security_level: str
    product: str | None = None
    region: str | None = None
    effective_at_ms: int = 0
    expires_at_ms: int = 0
    title: str | None = None
    section_path: str | None = None
    page: int | None = None


@dataclass(frozen=True, slots=True)
class ChunkView:
    """A stored chunk read back for knowledge governance / inspection."""

    chunk_id: str
    parent_id: str
    text: str
    title: str | None = None
    section_path: str | None = None
    page: int | None = None


@dataclass(frozen=True, slots=True)
class SkillManifest:
    """Level-1 skill metadata: the cheap, always-loaded catalog entry.

    Only ``name`` + ``description`` are surfaced to the model at startup so a
    large skill library costs a few tokens per skill (progressive disclosure).
    The body (instructions) and resources are loaded on demand.
    """

    name: str
    description: str


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    """Level-2 skill payload: the SKILL.md body loaded only when a skill matches.

    ``instructions`` is the Markdown body (operating procedure for the model).
    ``resources`` lists relative paths the body may reference; their contents
    are level-3 and read individually only when actually needed.
    """

    name: str
    description: str
    instructions: str
    resources: tuple[str, ...] = ()
