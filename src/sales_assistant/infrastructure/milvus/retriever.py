from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from pymilvus import AnnSearchRequest, AsyncMilvusClient

from sales_assistant.domain import Candidate, RecallSource, RetrievalFilters
from sales_assistant.infrastructure.milvus import schema as fields

_SECURITY_ORDER = {"normal": 0, "confidential": 1, "restricted": 2}

_OUTPUT_FIELDS = [
    fields.FIELD_PK,
    fields.FIELD_DOC_VERSION,
    fields.FIELD_PARENT,
    fields.FIELD_TEXT,
    fields.FIELD_TITLE,
    fields.FIELD_SECTION,
    fields.FIELD_PAGE,
]


def build_filter_expr(
    *,
    tenant_id: UUID,
    acl_tokens: Sequence[str],
    filters: RetrievalFilters,
) -> str:
    """Scalar pre-filter, applied BEFORE recall (rag-design.md 3.2)."""
    now_ms = int(time.time() * 1000)
    clauses = [
        f'{fields.FIELD_TENANT} == "{tenant_id}"',
        f'{fields.FIELD_STATUS} == "published"',
        f"{fields.FIELD_EFFECTIVE} <= {now_ms}",
        f"({fields.FIELD_EXPIRES} == 0 || {fields.FIELD_EXPIRES} > {now_ms})",
    ]
    if acl_tokens:
        tokens = ", ".join(f'"{t}"' for t in acl_tokens)
        clauses.append(f"array_contains_any({fields.FIELD_ACL}, [{tokens}])")
    allowed = [
        level
        for level, rank in _SECURITY_ORDER.items()
        if rank <= _SECURITY_ORDER.get(filters.max_security_level, 0)
    ]
    levels = ", ".join(f'"{lvl}"' for lvl in allowed)
    clauses.append(f"{fields.FIELD_SECURITY} in [{levels}]")
    if filters.knowledge_base_ids:
        kbs = ", ".join(f'"{kb}"' for kb in filters.knowledge_base_ids)
        clauses.append(f"{fields.FIELD_KB} in [{kbs}]")
    if filters.products:
        products = ", ".join(f'"{p}"' for p in filters.products)
        clauses.append(f"{fields.FIELD_PRODUCT} in [{products}]")
    if filters.regions:
        regions = ", ".join(f'"{r}"' for r in filters.regions)
        clauses.append(f"{fields.FIELD_REGION} in [{regions}]")
    return " && ".join(clauses)


def _to_candidate(hit: dict[str, Any], source: RecallSource) -> Candidate:
    entity = hit.get("entity", hit)
    return Candidate(
        chunk_id=str(hit.get("id", entity.get(fields.FIELD_PK, ""))),
        document_version_id=str(entity.get(fields.FIELD_DOC_VERSION, "")),
        parent_id=str(entity.get(fields.FIELD_PARENT, "")),
        text=str(entity.get(fields.FIELD_TEXT, "")),
        score=float(hit.get("distance", 0.0)),
        source=source,
        title=entity.get(fields.FIELD_TITLE) or None,
        section_path=entity.get(fields.FIELD_SECTION) or None,
        page=entity.get(fields.FIELD_PAGE),
    )


class MilvusRetriever:
    """Dual-route retriever over Milvus (ADR-0002).

    Scalar ACL/tenant/status/freshness filters are pushed into the search
    expression so recall never returns unauthorised chunks.
    """

    def __init__(self, uri: str, *, collection: str = fields.ACTIVE_ALIAS) -> None:
        self._uri = uri
        self._collection = collection
        self._client_instance: AsyncMilvusClient | None = None

    @property
    def _client(self) -> AsyncMilvusClient:
        # Lazily create the async client inside the running event loop; the grpc
        # aio channel binds to the current loop, so eager construction at
        # container-build time would attach it to the wrong loop.
        if self._client_instance is None:
            self._client_instance = AsyncMilvusClient(uri=self._uri)
        return self._client_instance

    async def dense_recall(
        self,
        *,
        tenant_id: UUID,
        query_vector: Sequence[float],
        acl_tokens: Sequence[str],
        filters: RetrievalFilters,
        top_k: int,
    ) -> list[Candidate]:
        expr = build_filter_expr(tenant_id=tenant_id, acl_tokens=acl_tokens, filters=filters)
        results = await self._client.search(
            collection_name=self._collection,
            data=[list(query_vector)],
            anns_field=fields.FIELD_DENSE,
            filter=expr,
            limit=top_k,
            output_fields=_OUTPUT_FIELDS,
            search_params={"metric_type": "COSINE", "params": {"ef": 128}},
        )
        return [_to_candidate(hit, RecallSource.DENSE) for hit in (results[0] if results else [])]

    async def bm25_recall(
        self,
        *,
        tenant_id: UUID,
        query_text: str,
        acl_tokens: Sequence[str],
        filters: RetrievalFilters,
        top_k: int,
    ) -> list[Candidate]:
        expr = build_filter_expr(tenant_id=tenant_id, acl_tokens=acl_tokens, filters=filters)
        results = await self._client.search(
            collection_name=self._collection,
            data=[query_text],
            anns_field=fields.FIELD_SPARSE,
            filter=expr,
            limit=top_k,
            output_fields=_OUTPUT_FIELDS,
            search_params={"metric_type": "BM25"},
        )
        return [_to_candidate(hit, RecallSource.BM25) for hit in (results[0] if results else [])]

    async def health_check(self) -> None:
        await self._client.list_collections()

    async def close(self) -> None:
        if self._client_instance is not None:
            await self._client_instance.close()


# Unused in the current recall path but kept for future weighted server-side
# hybrid search experiments.
__all__ = ["AnnSearchRequest", "MilvusRetriever", "build_filter_expr"]
