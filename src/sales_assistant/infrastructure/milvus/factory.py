from __future__ import annotations

from dataclasses import dataclass

from sales_assistant.domain import KnowledgeIndexer, Retriever
from sales_assistant.infrastructure.milvus.memory import (
    InMemoryKnowledgeIndexer,
    InMemoryRetriever,
)
from sales_assistant.settings import AppEnvironment, EmbeddingProvider, Settings


@dataclass(frozen=True, slots=True)
class RetrievalBackends:
    retriever: Retriever
    indexer: KnowledgeIndexer


def build_retrieval_backends(settings: Settings) -> RetrievalBackends:
    """Build the retriever + indexer pair.

    In test / mock mode they share one in-memory store so ingested chunks are
    immediately searchable. Otherwise both talk to Milvus over the async client.
    """
    if (
        settings.app_env is AppEnvironment.TEST
        or settings.embedding_provider is EmbeddingProvider.MOCK
    ):
        retriever = InMemoryRetriever()
        return RetrievalBackends(retriever=retriever, indexer=InMemoryKnowledgeIndexer(retriever))

    from sales_assistant.infrastructure.milvus.indexer import MilvusKnowledgeIndexer
    from sales_assistant.infrastructure.milvus.retriever import MilvusRetriever

    return RetrievalBackends(
        retriever=MilvusRetriever(settings.milvus_uri),
        indexer=MilvusKnowledgeIndexer(settings.milvus_uri),
    )
