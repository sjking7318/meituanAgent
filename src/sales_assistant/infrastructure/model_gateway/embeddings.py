from __future__ import annotations

from collections.abc import Sequence

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from sales_assistant.domain import Embedder, Reranker
from sales_assistant.settings import EmbeddingProvider, Settings


class MockEmbedder:
    """Deterministic embedder for tests / local runs without network."""

    def __init__(self, dim: int = 8) -> None:
        self._dim = dim

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            seed = float(sum(ord(c) for c in text) % 97 + 1)
            vectors.append([seed / (i + 1) for i in range(self._dim)])
        return vectors


class MockReranker:
    """Deterministic reranker: scores by lexical overlap with the query."""

    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        top_k: int,
    ) -> list[tuple[int, float]]:
        query_terms = set(query)
        scored = [
            (index, float(len(query_terms & set(doc)))) for index, doc in enumerate(documents)
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k]


class OpenAICompatibleEmbedder:
    """Embeddings via an OpenAI-compatible endpoint (e.g. Gitee AI)."""

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout_seconds: float) -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.2, max=2.0),
        retry=retry_if_exception_type(httpx.TransportError),
        reraise=True,
    )
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        response = await self._client.post(
            "/embeddings",
            json={"model": self._model, "input": list(texts)},
        )
        response.raise_for_status()
        payload = response.json()
        return [item["embedding"] for item in payload["data"]]

    async def close(self) -> None:
        await self._client.aclose()


class OpenAICompatibleReranker:
    """Reranking via an OpenAI-compatible rerank endpoint (e.g. Gitee AI)."""

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout_seconds: float) -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.2, max=2.0),
        retry=retry_if_exception_type(httpx.TransportError),
        reraise=True,
    )
    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        top_k: int,
    ) -> list[tuple[int, float]]:
        response = await self._client.post(
            "/rerank",
            json={
                "model": self._model,
                "query": query,
                "documents": list(documents),
                "top_n": top_k,
            },
        )
        response.raise_for_status()
        payload = response.json()
        results = [
            (int(item["index"]), float(item["relevance_score"])) for item in payload["results"]
        ]
        results.sort(key=lambda pair: pair[1], reverse=True)
        return results[:top_k]

    async def close(self) -> None:
        await self._client.aclose()


class DashScopeEmbedder:
    """Embeddings via DashScope native multimodal-embedding API.

    tongyi-embedding-vision-flash is not exposed through OpenAI-compatible mode,
    so it must use the native endpoint.
    """

    _PATH = "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout_seconds: float) -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.2, max=2.0),
        retry=retry_if_exception_type(httpx.TransportError),
        reraise=True,
    )
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        # The multimodal endpoint embeds one content payload per call.
        for text in texts:
            response = await self._client.post(
                self._PATH,
                json={"model": self._model, "input": {"contents": [{"text": text}]}},
            )
            response.raise_for_status()
            payload = response.json()
            vectors.append(payload["output"]["embeddings"][0]["embedding"])
        return vectors

    async def close(self) -> None:
        await self._client.aclose()


class DashScopeReranker:
    """Reranking via DashScope native text-rerank API."""

    _PATH = "/api/v1/services/rerank/text-rerank/text-rerank"

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout_seconds: float) -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.2, max=2.0),
        retry=retry_if_exception_type(httpx.TransportError),
        reraise=True,
    )
    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        top_k: int,
    ) -> list[tuple[int, float]]:
        response = await self._client.post(
            self._PATH,
            json={
                "model": self._model,
                "input": {"query": query, "documents": list(documents)},
                "parameters": {"return_documents": False, "top_n": top_k},
            },
        )
        response.raise_for_status()
        payload = response.json()
        results = [
            (int(item["index"]), float(item["relevance_score"]))
            for item in payload["output"]["results"]
        ]
        results.sort(key=lambda pair: pair[1], reverse=True)
        return results[:top_k]

    async def close(self) -> None:
        await self._client.aclose()


def build_embedder(settings: Settings) -> Embedder:
    provider = settings.embedding_provider
    if provider is EmbeddingProvider.MOCK or not settings.embedding_base_url:
        return MockEmbedder()
    assert settings.embedding_api_key is not None
    key = settings.embedding_api_key.get_secret_value()
    if provider is EmbeddingProvider.DASHSCOPE:
        return DashScopeEmbedder(
            base_url=settings.embedding_base_url,
            api_key=key,
            model=settings.embedding_model,
            timeout_seconds=settings.model_timeout_seconds,
        )
    return OpenAICompatibleEmbedder(
        base_url=settings.embedding_base_url,
        api_key=key,
        model=settings.embedding_model,
        timeout_seconds=settings.model_timeout_seconds,
    )


def build_reranker(settings: Settings) -> Reranker:
    provider = settings.embedding_provider
    if provider is EmbeddingProvider.MOCK or not settings.embedding_base_url:
        return MockReranker()
    assert settings.embedding_api_key is not None
    key = settings.embedding_api_key.get_secret_value()
    if provider is EmbeddingProvider.DASHSCOPE:
        return DashScopeReranker(
            base_url=settings.embedding_base_url,
            api_key=key,
            model=settings.rerank_model,
            timeout_seconds=settings.model_timeout_seconds,
        )
    return OpenAICompatibleReranker(
        base_url=settings.embedding_base_url,
        api_key=key,
        model=settings.rerank_model,
        timeout_seconds=settings.model_timeout_seconds,
    )
