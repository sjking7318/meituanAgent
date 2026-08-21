from __future__ import annotations

from typing import Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from sales_assistant.api.routes import Auth
from sales_assistant.application.ingestion_service import IngestionService


class _KnowledgeContainer(Protocol):
    ingestion_service: IngestionService


def _ingestion(request: Request) -> IngestionService:
    container = cast(_KnowledgeContainer, request.app.state.container)
    return container.ingestion_service


class CreateKnowledgeBaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)


class KnowledgeBaseResponse(BaseModel):
    id: UUID


class IngestDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1)
    content_type: str = "text/markdown"
    acl_tokens: list[str] = Field(default_factory=list)
    security_level: str = "normal"

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("title cannot be blank")
        return stripped


class IngestDocumentResponse(BaseModel):
    document_id: UUID
    document_version_id: UUID
    version: int
    chunk_count: int
    status: str


class KnowledgeBaseItem(BaseModel):
    id: UUID
    name: str
    status: str


class DocumentItem(BaseModel):
    id: UUID
    knowledge_base_id: UUID
    title: str
    status: str


class VersionItem(BaseModel):
    id: UUID
    version: int
    status: str
    chunk_count: int
    embedding_model: str
    error_code: str | None


class ChunkItem(BaseModel):
    chunk_id: str
    parent_id: str
    text: str
    title: str | None
    section_path: str | None
    page: int | None


router = APIRouter(prefix="/v1/knowledge", tags=["knowledge"])


@router.post(
    "/knowledge-bases",
    response_model=KnowledgeBaseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_knowledge_base(
    payload: CreateKnowledgeBaseRequest,
    context: Auth,
    request: Request,
) -> KnowledgeBaseResponse:
    kb_id = await _ingestion(request).create_knowledge_base(
        tenant_id=context.tenant_id,
        name=payload.name,
    )
    return KnowledgeBaseResponse(id=kb_id)


@router.post(
    "/knowledge-bases/{knowledge_base_id}/documents",
    response_model=IngestDocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def ingest_document(
    knowledge_base_id: UUID,
    payload: IngestDocumentRequest,
    context: Auth,
    request: Request,
) -> IngestDocumentResponse:
    result = await _ingestion(request).ingest_document(
        tenant_id=context.tenant_id,
        knowledge_base_id=knowledge_base_id,
        title=payload.title,
        content=payload.content,
        content_type=payload.content_type,
        acl_tokens=tuple(payload.acl_tokens),
        security_level=payload.security_level,
    )
    return IngestDocumentResponse(
        document_id=result.document_id,
        document_version_id=result.document_version_id,
        version=result.version,
        chunk_count=result.chunk_count,
        status=result.status,
    )


@router.get("/knowledge-bases", response_model=list[KnowledgeBaseItem])
async def list_knowledge_bases(context: Auth, request: Request) -> list[KnowledgeBaseItem]:
    records = await _ingestion(request).list_knowledge_bases(tenant_id=context.tenant_id)
    return [KnowledgeBaseItem(id=r.id, name=r.name, status=r.status) for r in records]


@router.get(
    "/knowledge-bases/{knowledge_base_id}/documents",
    response_model=list[DocumentItem],
)
async def list_documents(
    knowledge_base_id: UUID, context: Auth, request: Request
) -> list[DocumentItem]:
    records = await _ingestion(request).list_documents(
        tenant_id=context.tenant_id, knowledge_base_id=knowledge_base_id
    )
    return [
        DocumentItem(
            id=r.id,
            knowledge_base_id=r.knowledge_base_id,
            title=r.title,
            status=r.status,
        )
        for r in records
    ]


@router.get("/documents/{document_id}/versions", response_model=list[VersionItem])
async def list_versions(
    document_id: UUID, context: Auth, request: Request
) -> list[VersionItem]:
    records = await _ingestion(request).list_versions(
        tenant_id=context.tenant_id, document_id=document_id
    )
    return [
        VersionItem(
            id=r.id,
            version=r.version,
            status=r.status,
            chunk_count=r.chunk_count,
            embedding_model=r.embedding_model,
            error_code=r.error_code,
        )
        for r in records
    ]


@router.get(
    "/document-versions/{version_id}/chunks",
    response_model=list[ChunkItem],
)
async def list_version_chunks(
    version_id: UUID, context: Auth, request: Request
) -> list[ChunkItem]:
    chunks = await _ingestion(request).list_chunks(
        tenant_id=context.tenant_id, version_id=version_id
    )
    return [
        ChunkItem(
            chunk_id=c.chunk_id,
            parent_id=c.parent_id,
            text=c.text,
            title=c.title,
            section_path=c.section_path,
            page=c.page,
        )
        for c in chunks
    ]


@router.delete(
    "/document-versions/{version_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_document_version(
    version_id: UUID, context: Auth, request: Request
) -> Response:
    await _ingestion(request).delete_version(tenant_id=context.tenant_id, version_id=version_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
