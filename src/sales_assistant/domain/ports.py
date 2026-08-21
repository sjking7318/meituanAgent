from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Any, Protocol
from uuid import UUID

from sales_assistant.domain.entities import (
    AgentRun,
    Candidate,
    ChunkRecord,
    ChunkView,
    Conversation,
    ConversationSummary,
    Message,
    ModelRequest,
    ModelResponse,
    RetrievalFilters,
    StoredRunEvent,
)


class ModelGateway(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...


class Embedder(Protocol):
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class Reranker(Protocol):
    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        top_k: int,
    ) -> list[tuple[int, float]]:
        """Return (document_index, score) pairs ordered by descending score."""
        ...


class Retriever(Protocol):
    """Dual-route recall over an ACL-filtered knowledge index (ADR-0002).

    Implementations MUST apply tenant + ACL + status + freshness scalar filters
    BEFORE recall; recall-then-filter is forbidden (rag-design.md 3.2).
    """

    async def dense_recall(
        self,
        *,
        tenant_id: UUID,
        query_vector: Sequence[float],
        acl_tokens: Sequence[str],
        filters: RetrievalFilters,
        top_k: int,
    ) -> list[Candidate]: ...

    async def bm25_recall(
        self,
        *,
        tenant_id: UUID,
        query_text: str,
        acl_tokens: Sequence[str],
        filters: RetrievalFilters,
        top_k: int,
    ) -> list[Candidate]: ...

    async def health_check(self) -> None: ...

    async def close(self) -> None: ...


class KnowledgeIndexer(Protocol):
    """Writes chunk records into the vector index (rag-design.md 2, 7).

    ``ensure_ready`` creates the collection/alias if missing; ``index_chunks``
    upserts a document version's child chunks; ``delete_document_version``
    removes them for rollback / deletion.
    """

    async def ensure_ready(self, *, dense_dim: int) -> None: ...

    async def index_chunks(self, chunks: Sequence[ChunkRecord]) -> int: ...

    async def list_chunks(
        self,
        *,
        tenant_id: UUID,
        document_version_id: UUID,
        limit: int = 500,
    ) -> list[ChunkView]: ...

    async def delete_document_version(
        self, *, tenant_id: UUID, document_version_id: UUID
    ) -> None: ...

    async def health_check(self) -> None: ...

    async def close(self) -> None: ...


class ConversationLease(Protocol):
    async def ensure_valid(self) -> None: ...

    @property
    def fencing_token(self) -> int: ...


class LeaseManager(Protocol):
    def hold(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
    ) -> AbstractAsyncContextManager[ConversationLease]: ...

    async def health_check(self) -> None: ...

    async def close(self) -> None: ...


class RunEventStream(Protocol):
    async def append(
        self,
        run_id: UUID,
        event_type: str,
        data: dict[str, Any],
    ) -> StoredRunEvent: ...

    async def read(
        self,
        run_id: UUID,
        *,
        after_id: str,
        block_milliseconds: int,
        limit: int = 100,
    ) -> list[StoredRunEvent]: ...

    async def health_check(self) -> None: ...

    async def close(self) -> None: ...


class ConversationRepository(Protocol):
    async def add(self, conversation: Conversation) -> None: ...

    async def get(self, tenant_id: UUID, conversation_id: UUID) -> Conversation | None: ...

    async def bump_version(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
        expected_version: int,
    ) -> int: ...

    async def lock_at_version(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
        expected_version: int,
    ) -> None: ...


class MessageRepository(Protocol):
    async def add(self, message: Message) -> None: ...

    async def list(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
        *,
        limit: int,
        before_sequence: int | None = None,
    ) -> list[Message]: ...

    async def get(self, tenant_id: UUID, message_id: UUID) -> Message | None: ...


class ConversationSummaryRepository(Protocol):
    """Versioned rolling-summary storage (memory-design.md 3, 5)."""

    async def add(self, summary: ConversationSummary) -> None: ...

    async def latest(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
    ) -> ConversationSummary | None: ...


class RunRepository(Protocol):
    async def add(self, run: AgentRun) -> None: ...

    async def get(self, tenant_id: UUID, run_id: UUID) -> AgentRun | None: ...

    async def get_by_idempotency_key(
        self,
        tenant_id: UUID,
        idempotency_key: str,
    ) -> AgentRun | None: ...

    async def save(self, run: AgentRun) -> None: ...


class UnitOfWork(Protocol):
    conversations: ConversationRepository
    messages: MessageRepository
    summaries: ConversationSummaryRepository
    runs: RunRepository

    async def __aenter__(self) -> UnitOfWork: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...
