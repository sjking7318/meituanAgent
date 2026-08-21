from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sales_assistant.domain import ChunkRecord, ChunkView
from sales_assistant.infrastructure.milvus import schema as fields


def _to_row(chunk: ChunkRecord) -> dict[str, object]:
    return {
        fields.FIELD_PK: chunk.chunk_id,
        fields.FIELD_TEXT: chunk.text,
        fields.FIELD_DENSE: list(chunk.dense),
        fields.FIELD_PARENT: chunk.parent_id,
        fields.FIELD_DOC_VERSION: str(chunk.document_version_id),
        fields.FIELD_TENANT: str(chunk.tenant_id),
        fields.FIELD_KB: str(chunk.knowledge_base_id),
        fields.FIELD_ACL: list(chunk.acl_tokens),
        fields.FIELD_STATUS: chunk.status,
        fields.FIELD_SECURITY: chunk.security_level,
        fields.FIELD_PRODUCT: chunk.product or "",
        fields.FIELD_REGION: chunk.region or "",
        fields.FIELD_EFFECTIVE: chunk.effective_at_ms,
        fields.FIELD_EXPIRES: chunk.expires_at_ms,
        fields.FIELD_TITLE: chunk.title or "",
        fields.FIELD_SECTION: chunk.section_path or "",
        fields.FIELD_PAGE: chunk.page or 0,
    }


class MilvusKnowledgeIndexer:
    """Writes chunks into Milvus ``knowledge_chunks`` (ADR-0002).

    The collection is created lazily via the sync client (DDL), while inserts
    and deletes use the async client to fit the application event loop.
    """

    def __init__(self, uri: str, *, collection: str = fields.COLLECTION) -> None:
        self._uri = uri
        self._collection = collection
        self._async_client: object | None = None

    def _sync_client(self) -> object:
        from pymilvus import MilvusClient

        return MilvusClient(uri=self._uri)

    def _client(self) -> object:
        if self._async_client is None:
            from pymilvus import AsyncMilvusClient

            self._async_client = AsyncMilvusClient(uri=self._uri)
        return self._async_client

    async def ensure_ready(self, *, dense_dim: int) -> None:
        client = self._sync_client()
        if client.has_collection(self._collection):  # type: ignore[attr-defined]
            self._ensure_alias(client)
            return
        schema = fields.build_schema(client, dense_dim=dense_dim)
        index_params = fields.build_index_params(client)
        client.create_collection(  # type: ignore[attr-defined]
            collection_name=self._collection,
            schema=schema,
            index_params=index_params,
        )
        self._ensure_alias(client)

    def _ensure_alias(self, client: object) -> None:
        # Retrieval queries the ``knowledge_active`` alias; point it at the
        # collection so recall works after ingestion (rag-design.md 7).
        try:
            aliases = client.list_aliases(collection_name=self._collection)  # type: ignore[attr-defined]
            existing = aliases.get("aliases", []) if isinstance(aliases, dict) else []
        except Exception:
            existing = []
        if fields.ACTIVE_ALIAS not in existing:
            client.create_alias(  # type: ignore[attr-defined]
                collection_name=self._collection, alias=fields.ACTIVE_ALIAS
            )

    async def index_chunks(self, chunks: Sequence[ChunkRecord]) -> int:
        if not chunks:
            return 0
        client = self._client()
        rows = [_to_row(c) for c in chunks]
        await client.upsert(collection_name=self._collection, data=rows)  # type: ignore[attr-defined]
        await client.load_collection(collection_name=self._collection)  # type: ignore[attr-defined]
        return len(rows)

    async def list_chunks(
        self,
        *,
        tenant_id: UUID,
        document_version_id: UUID,
        limit: int = 500,
    ) -> list[ChunkView]:
        client = self._client()
        expr = (
            f'{fields.FIELD_TENANT} == "{tenant_id}" '
            f'&& {fields.FIELD_DOC_VERSION} == "{document_version_id}"'
        )
        rows = await client.query(  # type: ignore[attr-defined]
            collection_name=self._collection,
            filter=expr,
            output_fields=[
                fields.FIELD_PK,
                fields.FIELD_PARENT,
                fields.FIELD_TEXT,
                fields.FIELD_TITLE,
                fields.FIELD_SECTION,
                fields.FIELD_PAGE,
            ],
            limit=limit,
            consistency_level="Strong",
        )
        views = [
            ChunkView(
                chunk_id=str(row.get(fields.FIELD_PK, "")),
                parent_id=str(row.get(fields.FIELD_PARENT, "")),
                text=str(row.get(fields.FIELD_TEXT, "")),
                title=(row.get(fields.FIELD_TITLE) or None),
                section_path=(row.get(fields.FIELD_SECTION) or None),
                page=(row.get(fields.FIELD_PAGE) or None),
            )
            for row in rows
        ]
        # Restore parent/child order lost by the vector store (…:p0:c0 < :p0:c1).
        views.sort(key=lambda v: v.chunk_id)
        return views

    async def delete_document_version(self, *, tenant_id: UUID, document_version_id: UUID) -> None:
        client = self._client()
        expr = (
            f'{fields.FIELD_TENANT} == "{tenant_id}" '
            f'&& {fields.FIELD_DOC_VERSION} == "{document_version_id}"'
        )
        await client.delete(collection_name=self._collection, filter=expr)  # type: ignore[attr-defined]

    async def health_check(self) -> None:
        client = self._client()
        await client.list_collections()  # type: ignore[attr-defined]

    async def close(self) -> None:
        if self._async_client is not None:
            await self._async_client.close()  # type: ignore[attr-defined]
