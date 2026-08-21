from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sales_assistant.domain import utc_now
from sales_assistant.infrastructure.mysql.models import (
    DocumentRecord,
    DocumentVersionRecord,
    KnowledgeBaseRecord,
)


class KnowledgeRepository:
    """CRUD for knowledge bases, documents and versions (T-201)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_knowledge_base(self, record: KnowledgeBaseRecord) -> None:
        self._session.add(record)
        await self._session.flush()

    async def get_knowledge_base(self, tenant_id: UUID, kb_id: UUID) -> KnowledgeBaseRecord | None:
        stmt = select(KnowledgeBaseRecord).where(
            KnowledgeBaseRecord.id == kb_id,
            KnowledgeBaseRecord.tenant_id == tenant_id,
        )
        record: KnowledgeBaseRecord | None = await self._session.scalar(stmt)
        return record

    async def add_document(self, record: DocumentRecord) -> None:
        self._session.add(record)
        await self._session.flush()

    async def get_document(self, tenant_id: UUID, document_id: UUID) -> DocumentRecord | None:
        stmt = select(DocumentRecord).where(
            DocumentRecord.id == document_id,
            DocumentRecord.tenant_id == tenant_id,
        )
        document: DocumentRecord | None = await self._session.scalar(stmt)
        return document

    async def next_version_number(self, document_id: UUID) -> int:
        stmt = select(DocumentVersionRecord.version).where(
            DocumentVersionRecord.document_id == document_id
        )
        versions = list((await self._session.scalars(stmt)).all())
        return (max(versions) + 1) if versions else 1

    async def add_version(self, record: DocumentVersionRecord) -> None:
        self._session.add(record)
        await self._session.flush()

    async def get_version(self, tenant_id: UUID, version_id: UUID) -> DocumentVersionRecord | None:
        stmt = select(DocumentVersionRecord).where(
            DocumentVersionRecord.id == version_id,
            DocumentVersionRecord.tenant_id == tenant_id,
        )
        version: DocumentVersionRecord | None = await self._session.scalar(stmt)
        return version

    async def mark_version(
        self,
        version: DocumentVersionRecord,
        *,
        status: str,
        chunk_count: int | None = None,
        error_code: str | None = None,
    ) -> None:
        version.status = status
        if chunk_count is not None:
            version.chunk_count = chunk_count
        version.error_code = error_code
        version.updated_at = utc_now()
        await self._session.flush()

    async def list_knowledge_bases(self, tenant_id: UUID) -> list[KnowledgeBaseRecord]:
        stmt = (
            select(KnowledgeBaseRecord)
            .where(KnowledgeBaseRecord.tenant_id == tenant_id)
            .order_by(KnowledgeBaseRecord.created_at.desc())
        )
        return list((await self._session.scalars(stmt)).all())

    async def list_documents(
        self, tenant_id: UUID, knowledge_base_id: UUID
    ) -> list[DocumentRecord]:
        stmt = (
            select(DocumentRecord)
            .where(
                DocumentRecord.tenant_id == tenant_id,
                DocumentRecord.knowledge_base_id == knowledge_base_id,
            )
            .order_by(DocumentRecord.created_at.desc())
        )
        return list((await self._session.scalars(stmt)).all())

    async def list_versions(
        self, tenant_id: UUID, document_id: UUID
    ) -> list[DocumentVersionRecord]:
        stmt = (
            select(DocumentVersionRecord)
            .where(
                DocumentVersionRecord.tenant_id == tenant_id,
                DocumentVersionRecord.document_id == document_id,
            )
            .order_by(DocumentVersionRecord.version.desc())
        )
        return list((await self._session.scalars(stmt)).all())

    async def delete_version(self, version: DocumentVersionRecord) -> None:
        await self._session.execute(
            delete(DocumentVersionRecord).where(DocumentVersionRecord.id == version.id)
        )
        await self._session.flush()
