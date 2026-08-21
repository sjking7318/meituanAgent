from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sales_assistant.domain import (
    ChunkRecord,
    ChunkView,
    Embedder,
    KnowledgeIndexer,
    ResourceNotFoundError,
    utc_now,
)
from sales_assistant.domain.entities import new_id
from sales_assistant.infrastructure.mysql.knowledge_repository import KnowledgeRepository
from sales_assistant.infrastructure.mysql.models import (
    DocumentRecord,
    DocumentVersionRecord,
    KnowledgeBaseRecord,
)
from sales_assistant.ingestion.chunker import CHUNKER_VERSION, ParentChildChunker
from sales_assistant.ingestion.parser import PARSER_VERSION, TextParser

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class IngestionResult:
    document_id: UUID
    document_version_id: UUID
    version: int
    chunk_count: int
    status: str


class IngestionService:
    """Synchronous document ingestion (rag-design.md 2).

    parse -> Parent-Child chunk -> embed -> index into Milvus -> mark published.
    Kafka-driven async ingestion is a later refinement; the pipeline stages are
    already isolated so they can move to a worker unchanged.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        indexer: KnowledgeIndexer,
        *,
        embedding_model: str,
        embed_batch_size: int = 32,
    ) -> None:
        self._session_factory = session_factory
        self._embedder = embedder
        self._indexer = indexer
        self._embedding_model = embedding_model
        self._embed_batch_size = embed_batch_size
        self._parser = TextParser()
        self._chunker = ParentChildChunker()

    async def create_knowledge_base(self, *, tenant_id: UUID, name: str) -> UUID:
        record = KnowledgeBaseRecord(
            id=new_id(), tenant_id=tenant_id, name=name, status="active", created_at=utc_now()
        )
        async with self._session_factory() as session:
            await KnowledgeRepository(session).add_knowledge_base(record)
            await session.commit()
        return record.id

    async def ingest_document(
        self,
        *,
        tenant_id: UUID,
        knowledge_base_id: UUID,
        title: str,
        content: str,
        content_type: str = "text/markdown",
        acl_tokens: tuple[str, ...] = (),
        security_level: str = "normal",
        logical_id: str | None = None,
    ) -> IngestionResult:
        parsed = self._parser.parse(content, content_type=content_type)
        source_hash = hashlib.sha256(content.encode()).hexdigest()

        async with self._session_factory() as session:
            repo = KnowledgeRepository(session)
            kb = await repo.get_knowledge_base(tenant_id, knowledge_base_id)
            if kb is None:
                raise ResourceNotFoundError("knowledge base not found")

            document = DocumentRecord(
                id=new_id(),
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                logical_id=logical_id or hashlib.sha1(title.encode()).hexdigest()[:16],  # noqa: S324
                title=title,
                status="active",
                created_at=utc_now(),
            )
            await repo.add_document(document)
            version_number = await repo.next_version_number(document.id)
            version = DocumentVersionRecord(
                id=new_id(),
                tenant_id=tenant_id,
                document_id=document.id,
                version=version_number,
                source_hash=source_hash,
                parser_version=PARSER_VERSION,
                chunker_version=CHUNKER_VERSION,
                embedding_model=self._embedding_model,
                language=parsed.language,
                security_level=security_level,
                acl_tokens=list(acl_tokens),
                chunk_count=0,
                status="processing",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            await repo.add_version(version)
            await session.commit()
            version_id = version.id

        try:
            chunk_count = await self._process(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                document_version_id=version_id,
                title=title,
                text=parsed.text,
                acl_tokens=acl_tokens,
                security_level=security_level,
            )
        except Exception as error:
            await self._update_status(tenant_id, version_id, status="failed", error="INGEST_FAILED")
            logger.exception("ingestion_failed", document_version_id=str(version_id))
            raise error

        await self._update_status(
            tenant_id, version_id, status="published", chunk_count=chunk_count
        )
        return IngestionResult(
            document_id=document.id,
            document_version_id=version_id,
            version=version_number,
            chunk_count=chunk_count,
            status="published",
        )

    async def _process(
        self,
        *,
        tenant_id: UUID,
        knowledge_base_id: UUID,
        document_version_id: UUID,
        title: str,
        text: str,
        acl_tokens: tuple[str, ...],
        security_level: str,
    ) -> int:
        parents = self._chunker.chunk(text, document_version_id=str(document_version_id))
        children = [child for parent in parents for child in parent.children]
        if not children:
            return 0

        now_ms = int(time.time() * 1000)
        records: list[ChunkRecord] = []
        for start in range(0, len(children), self._embed_batch_size):
            batch = children[start : start + self._embed_batch_size]
            vectors = await self._embedder.embed([c.text for c in batch])
            dim = len(vectors[0]) if vectors else 0
            await self._indexer.ensure_ready(dense_dim=dim)
            for child, vector in zip(batch, vectors, strict=True):
                records.append(
                    ChunkRecord(
                        chunk_id=child.child_id,
                        parent_id=child.parent_id,
                        text=child.text,
                        dense=vector,
                        tenant_id=tenant_id,
                        knowledge_base_id=knowledge_base_id,
                        document_version_id=document_version_id,
                        acl_tokens=acl_tokens,
                        status="published",
                        security_level=security_level,
                        effective_at_ms=now_ms,
                        expires_at_ms=0,
                        title=title,
                        section_path=child.section_path,
                        page=child.page,
                    )
                )
        return await self._indexer.index_chunks(records)

    async def _update_status(
        self,
        tenant_id: UUID,
        version_id: UUID,
        *,
        status: str,
        chunk_count: int | None = None,
        error: str | None = None,
    ) -> None:
        async with self._session_factory() as session:
            repo = KnowledgeRepository(session)
            version = await repo.get_version(tenant_id, version_id)
            if version is None:
                return
            await repo.mark_version(
                version, status=status, chunk_count=chunk_count, error_code=error
            )
            await session.commit()

    async def list_knowledge_bases(self, *, tenant_id: UUID) -> list[KnowledgeBaseRecord]:
        async with self._session_factory() as session:
            return await KnowledgeRepository(session).list_knowledge_bases(tenant_id)

    async def list_documents(
        self, *, tenant_id: UUID, knowledge_base_id: UUID
    ) -> list[DocumentRecord]:
        async with self._session_factory() as session:
            repo = KnowledgeRepository(session)
            if await repo.get_knowledge_base(tenant_id, knowledge_base_id) is None:
                raise ResourceNotFoundError("knowledge base not found")
            return await repo.list_documents(tenant_id, knowledge_base_id)

    async def list_versions(
        self, *, tenant_id: UUID, document_id: UUID
    ) -> list[DocumentVersionRecord]:
        async with self._session_factory() as session:
            repo = KnowledgeRepository(session)
            if await repo.get_document(tenant_id, document_id) is None:
                raise ResourceNotFoundError("document not found")
            return await repo.list_versions(tenant_id, document_id)

    async def list_chunks(
        self, *, tenant_id: UUID, version_id: UUID, limit: int = 500
    ) -> list[ChunkView]:
        """Read back the stored child chunks of a version for governance."""
        async with self._session_factory() as session:
            repo = KnowledgeRepository(session)
            if await repo.get_version(tenant_id, version_id) is None:
                raise ResourceNotFoundError("document version not found")
        return await self._indexer.list_chunks(
            tenant_id=tenant_id, document_version_id=version_id, limit=limit
        )

    async def delete_version(self, *, tenant_id: UUID, version_id: UUID) -> None:
        """Delete a document version and its chunks from the vector index."""
        async with self._session_factory() as session:
            repo = KnowledgeRepository(session)
            version = await repo.get_version(tenant_id, version_id)
            if version is None:
                raise ResourceNotFoundError("document version not found")
            await repo.delete_version(version)
            await session.commit()
        # Vector deletion after the DB commit; the index tolerates re-deletion.
        await self._indexer.delete_document_version(
            tenant_id=tenant_id, document_version_id=version_id
        )
