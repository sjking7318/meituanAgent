from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

import structlog

from sales_assistant.domain import (
    Candidate,
    Embedder,
    Evidence,
    RecallSource,
    Reranker,
    RetrievalFilters,
    Retriever,
)

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    """Tunable retrieval parameters (from env Settings, rag-design.md 2.1)."""

    rrf_k: int = 60
    weight_dense: float = 1.0
    weight_bm25: float = 1.0
    recall_top_k: int = 50
    rerank_input: int = 40
    final_evidence: int = 8


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    evidence: list[Evidence]
    dense_count: int
    bm25_count: int
    fused_count: int
    reranked: bool


def reciprocal_rank_fusion(
    ranked_lists: Sequence[tuple[Sequence[Candidate], float]],
    *,
    k: int,
) -> list[tuple[Candidate, float]]:
    """Weighted RRF over multiple ranked candidate lists (rag-design.md 3.1).

    score(d) = sum_i ( weight_i / (k + rank_i(d)) ), fused by chunk_id.
    """
    scores: dict[str, float] = {}
    best: dict[str, Candidate] = {}
    for candidates, weight in ranked_lists:
        for rank, candidate in enumerate(candidates):
            key = candidate.chunk_id
            scores[key] = scores.get(key, 0.0) + weight / (k + rank + 1)
            # Keep the representative candidate (prefer one carrying more text).
            if key not in best or len(candidate.text) > len(best[key].text):
                best[key] = candidate
    fused = [(best[key], score) for key, score in scores.items()]
    fused.sort(key=lambda pair: pair[1], reverse=True)
    return fused


class RetrievalService:
    """Agentic RAG recall pipeline (rag-design.md 3).

    query embed -> (dense || bm25) recall -> weighted RRF -> dedup ->
    cross-encoder rerank -> evidence packing.
    """

    def __init__(
        self,
        retriever: Retriever,
        embedder: Embedder,
        reranker: Reranker,
        config: RetrievalConfig,
    ) -> None:
        self._retriever = retriever
        self._embedder = embedder
        self._reranker = reranker
        self._config = config

    async def retrieve(
        self,
        *,
        tenant_id: UUID,
        query: str,
        acl_tokens: Sequence[str] = (),
        filters: RetrievalFilters | None = None,
    ) -> RetrievalResult:
        filters = filters or RetrievalFilters()
        cfg = self._config

        query_vector = (await self._embedder.embed([query]))[0]
        dense = await self._retriever.dense_recall(
            tenant_id=tenant_id,
            query_vector=query_vector,
            acl_tokens=acl_tokens,
            filters=filters,
            top_k=cfg.recall_top_k,
        )
        bm25 = await self._retriever.bm25_recall(
            tenant_id=tenant_id,
            query_text=query,
            acl_tokens=acl_tokens,
            filters=filters,
            top_k=cfg.recall_top_k,
        )

        fused = reciprocal_rank_fusion(
            [(dense, cfg.weight_dense), (bm25, cfg.weight_bm25)],
            k=cfg.rrf_k,
        )
        candidates = [candidate for candidate, _ in fused[: cfg.rerank_input]]

        reranked = False
        if candidates:
            try:
                order = await self._reranker.rerank(
                    query,
                    [c.text for c in candidates],
                    top_k=cfg.final_evidence,
                )
                candidates = [candidates[idx] for idx, _ in order]
                reranked = True
            except Exception:
                logger.warning("rerank_unavailable_degraded_to_rrf")
                candidates = candidates[: cfg.final_evidence]
        evidence = [self._to_evidence(c) for c in candidates[: cfg.final_evidence]]
        return RetrievalResult(
            evidence=evidence,
            dense_count=len(dense),
            bm25_count=len(bm25),
            fused_count=len(fused),
            reranked=reranked,
        )

    @staticmethod
    def _to_evidence(candidate: Candidate) -> Evidence:
        return Evidence(
            chunk_id=candidate.chunk_id,
            document_version_id=candidate.document_version_id,
            text=candidate.text,
            score=candidate.score,
            title=candidate.title,
            section_path=candidate.section_path,
            page=candidate.page,
        )


__all__ = [
    "RecallSource",
    "RetrievalConfig",
    "RetrievalResult",
    "RetrievalService",
    "reciprocal_rank_fusion",
]
