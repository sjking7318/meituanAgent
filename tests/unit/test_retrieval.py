from __future__ import annotations

from uuid import uuid4

import pytest

from sales_assistant.application.retrieval_service import (
    RetrievalConfig,
    RetrievalService,
    reciprocal_rank_fusion,
)
from sales_assistant.domain import Candidate, RecallSource, RetrievalFilters
from sales_assistant.infrastructure.milvus.memory import IndexedChunk, InMemoryRetriever
from sales_assistant.infrastructure.model_gateway.embeddings import MockEmbedder, MockReranker

pytestmark = pytest.mark.asyncio


def _cand(chunk_id: str, text: str, source: RecallSource) -> Candidate:
    return Candidate(
        chunk_id=chunk_id,
        document_version_id="dv1",
        parent_id="p1",
        text=text,
        score=1.0,
        source=source,
    )


async def test_rrf_fuses_and_orders() -> None:
    dense = [_cand("a", "aaa", RecallSource.DENSE), _cand("b", "bbb", RecallSource.DENSE)]
    bm25 = [_cand("b", "bbb", RecallSource.BM25), _cand("c", "ccc", RecallSource.BM25)]
    fused = reciprocal_rank_fusion([(dense, 1.0), (bm25, 1.0)], k=60)
    ids = [c.chunk_id for c, _ in fused]
    # "b" appears in both lists so should rank first.
    assert ids[0] == "b"
    assert set(ids) == {"a", "b", "c"}


async def test_rrf_weight_influences_order() -> None:
    dense = [_cand("a", "aaa", RecallSource.DENSE)]
    bm25 = [_cand("b", "bbb", RecallSource.BM25)]
    fused = reciprocal_rank_fusion([(dense, 5.0), (bm25, 1.0)], k=60)
    assert fused[0][0].chunk_id == "a"


async def test_in_memory_retriever_tenant_isolation() -> None:
    tenant_a, tenant_b = uuid4(), uuid4()
    retriever = InMemoryRetriever()
    retriever.add(IndexedChunk("c1", "dv1", "p1", "销售话术推荐内容", tenant_a))
    retriever.add(IndexedChunk("c2", "dv2", "p2", "销售话术推荐内容", tenant_b))

    hits = await retriever.bm25_recall(
        tenant_id=tenant_a,
        query_text="销售话术",
        acl_tokens=[],
        filters=RetrievalFilters(),
        top_k=10,
    )
    assert {h.chunk_id for h in hits} == {"c1"}


async def test_in_memory_retriever_acl_filter() -> None:
    tenant = uuid4()
    retriever = InMemoryRetriever()
    retriever.add(IndexedChunk("c1", "dv1", "p1", "受限销售话术", tenant, acl_tokens=("team-x",)))
    # Caller without the token must not see the chunk.
    hits = await retriever.bm25_recall(
        tenant_id=tenant,
        query_text="销售话术",
        acl_tokens=["team-y"],
        filters=RetrievalFilters(),
        top_k=10,
    )
    assert hits == []
    # Caller with the token sees it.
    ok = await retriever.bm25_recall(
        tenant_id=tenant,
        query_text="销售话术",
        acl_tokens=["team-x"],
        filters=RetrievalFilters(),
        top_k=10,
    )
    assert {h.chunk_id for h in ok} == {"c1"}


async def test_retrieval_service_returns_packed_evidence() -> None:
    tenant = uuid4()
    retriever = InMemoryRetriever()
    for i in range(3):
        retriever.add(IndexedChunk(f"c{i}", f"dv{i}", f"p{i}", f"销售话术推荐要点 {i}", tenant))
    service = RetrievalService(
        retriever, MockEmbedder(), MockReranker(), RetrievalConfig(final_evidence=2)
    )
    result = await service.retrieve(tenant_id=tenant, query="销售话术推荐")
    assert len(result.evidence) == 2
    assert result.reranked is True
    assert result.dense_count > 0


async def test_retrieval_service_empty_when_no_match() -> None:
    tenant = uuid4()
    service = RetrievalService(
        InMemoryRetriever(), MockEmbedder(), MockReranker(), RetrievalConfig()
    )
    result = await service.retrieve(tenant_id=tenant, query="任何问题")
    assert result.evidence == []
