from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from sales_assistant.domain import Candidate, ChunkRecord, ChunkView, RecallSource, RetrievalFilters

_SECURITY_ORDER = {"normal": 0, "confidential": 1, "restricted": 2}


@dataclass(slots=True)
class IndexedChunk:
    """A chunk stored in the in-memory retriever (tests / local Mock)."""

    chunk_id: str
    document_version_id: str
    parent_id: str
    text: str
    tenant_id: UUID
    acl_tokens: tuple[str, ...] = ()
    security_level: str = "normal"
    title: str | None = None
    section_path: str | None = None
    page: int | None = None


@dataclass(slots=True)
class InMemoryRetriever:
    """Deterministic retriever for unit tests and local runs.

    Applies the same tenant + ACL + security pre-filter contract as Milvus so
    permission tests are meaningful without a live cluster.
    """

    chunks: list[IndexedChunk] = field(default_factory=list)

    def add(self, chunk: IndexedChunk) -> None:
        self.chunks.append(chunk)

    def _visible(
        self,
        tenant_id: UUID,
        acl_tokens: Sequence[str],
        filters: RetrievalFilters,
    ) -> list[IndexedChunk]:
        allowed = _SECURITY_ORDER.get(filters.max_security_level, 0)
        token_set = set(acl_tokens)
        result = []
        for chunk in self.chunks:
            if chunk.tenant_id != tenant_id:
                continue
            if _SECURITY_ORDER.get(chunk.security_level, 0) > allowed:
                continue
            if chunk.acl_tokens and not (token_set & set(chunk.acl_tokens)):
                continue
            result.append(chunk)
        return result

    async def dense_recall(
        self,
        *,
        tenant_id: UUID,
        query_vector: Sequence[float],
        acl_tokens: Sequence[str],
        filters: RetrievalFilters,
        top_k: int,
    ) -> list[Candidate]:
        # Vector is opaque here; rank by text length as a stable proxy.
        visible = self._visible(tenant_id, acl_tokens, filters)
        ranked = sorted(visible, key=lambda c: len(c.text), reverse=True)[:top_k]
        return [self._candidate(c, 1.0 / (i + 1), RecallSource.DENSE) for i, c in enumerate(ranked)]

    async def bm25_recall(
        self,
        *,
        tenant_id: UUID,
        query_text: str,
        acl_tokens: Sequence[str],
        filters: RetrievalFilters,
        top_k: int,
    ) -> list[Candidate]:
        visible = self._visible(tenant_id, acl_tokens, filters)
        terms = set(query_text)
        scored = [(c, float(len(terms & set(c.text)))) for c in visible]
        scored = [pair for pair in scored if pair[1] > 0]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [self._candidate(c, score, RecallSource.BM25) for c, score in scored[:top_k]]

    async def health_check(self) -> None:
        return None

    async def close(self) -> None:
        return None

    @staticmethod
    def _candidate(chunk: IndexedChunk, score: float, source: RecallSource) -> Candidate:
        return Candidate(
            chunk_id=chunk.chunk_id,
            document_version_id=chunk.document_version_id,
            parent_id=chunk.parent_id,
            text=chunk.text,
            score=score,
            source=source,
            title=chunk.title,
            section_path=chunk.section_path,
            page=chunk.page,
        )


class InMemoryKnowledgeIndexer:
    """KnowledgeIndexer backed by an InMemoryRetriever (tests / local Mock).

    Ingested chunks become immediately searchable via the shared retriever,
    enabling end-to-end ingest->ask flows without a live Milvus.
    """

    def __init__(self, retriever: InMemoryRetriever) -> None:
        self._retriever = retriever

    async def ensure_ready(self, *, dense_dim: int) -> None:
        return None

    async def index_chunks(self, chunks: Sequence[ChunkRecord]) -> int:
        for chunk in chunks:
            self._retriever.add(
                IndexedChunk(
                    chunk_id=chunk.chunk_id,
                    document_version_id=str(chunk.document_version_id),
                    parent_id=chunk.parent_id,
                    text=chunk.text,
                    tenant_id=chunk.tenant_id,
                    acl_tokens=tuple(chunk.acl_tokens),
                    security_level=chunk.security_level,
                    title=chunk.title,
                    section_path=chunk.section_path,
                    page=chunk.page,
                )
            )
        return len(chunks)

    async def list_chunks(
        self,
        *,
        tenant_id: UUID,
        document_version_id: UUID,
        limit: int = 500,
    ) -> list[ChunkView]:
        dv = str(document_version_id)
        views = [
            ChunkView(
                chunk_id=c.chunk_id,
                parent_id=c.parent_id,
                text=c.text,
                title=c.title,
                section_path=c.section_path,
                page=c.page,
            )
            for c in self._retriever.chunks
            if c.tenant_id == tenant_id and c.document_version_id == dv
        ]
        views.sort(key=lambda v: v.chunk_id)
        return views[:limit]

    async def delete_document_version(self, *, tenant_id: UUID, document_version_id: UUID) -> None:
        dv = str(document_version_id)
        self._retriever.chunks = [
            c
            for c in self._retriever.chunks
            if not (c.tenant_id == tenant_id and c.document_version_id == dv)
        ]

    async def health_check(self) -> None:
        return None

    async def close(self) -> None:
        return None
