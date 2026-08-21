from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

from sales_assistant.application.ingestion_service import IngestionService
from sales_assistant.domain import AuthContext, ResourceNotFoundError
from sales_assistant.main import Container

pytestmark = pytest.mark.asyncio


def _headers(context: AuthContext, idem: str | None = None) -> dict[str, str]:
    headers = {"X-Tenant-ID": str(context.tenant_id), "X-User-ID": str(context.user_id)}
    if idem is not None:
        headers["Idempotency-Key"] = idem
    return headers


async def test_ingest_service_indexes_chunks(
    container: Container, auth_context: AuthContext
) -> None:
    service: IngestionService = container.ingestion_service
    kb_id = await service.create_knowledge_base(tenant_id=auth_context.tenant_id, name="政策库")
    result = await service.ingest_document(
        tenant_id=auth_context.tenant_id,
        knowledge_base_id=kb_id,
        title="佣金政策",
        content="# 佣金政策\n\n新商家首月免佣金，第二月起按 3% 收取。",
    )
    assert result.status == "published"
    assert result.chunk_count >= 1


async def test_list_chunks_carries_title_and_section(
    container: Container, auth_context: AuthContext
) -> None:
    service: IngestionService = container.ingestion_service
    kb_id = await service.create_knowledge_base(tenant_id=auth_context.tenant_id, name="政策库")
    result = await service.ingest_document(
        tenant_id=auth_context.tenant_id,
        knowledge_base_id=kb_id,
        title="佣金政策",
        content="# 佣金政策\n\n新商家首月免佣金，第二月起按 3% 收取。",
    )
    chunks = await service.list_chunks(
        tenant_id=auth_context.tenant_id, version_id=result.document_version_id
    )
    assert len(chunks) == result.chunk_count
    # Title is now pushed down to every chunk (fixes citation.title == None).
    assert all(c.title == "佣金政策" for c in chunks)
    assert any(c.section_path for c in chunks)
    # chunk_id order is stable parent/child (…:p0:c0).
    assert chunks[0].chunk_id.endswith(":c0")


async def test_list_chunks_missing_version_raises(
    container: Container, auth_context: AuthContext
) -> None:
    service: IngestionService = container.ingestion_service
    with pytest.raises(ResourceNotFoundError):
        await service.list_chunks(tenant_id=auth_context.tenant_id, version_id=uuid4())


async def test_ingest_missing_kb_raises(container: Container, auth_context: AuthContext) -> None:
    service: IngestionService = container.ingestion_service
    with pytest.raises(ResourceNotFoundError):
        await service.ingest_document(
            tenant_id=auth_context.tenant_id,
            knowledge_base_id=uuid4(),
            title="x",
            content="内容",
        )


async def test_end_to_end_ingest_then_ask(client: AsyncClient, auth_context: AuthContext) -> None:
    # 1. Create knowledge base.
    kb_resp = await client.post(
        "/v1/knowledge/knowledge-bases",
        json={"name": "销售政策库"},
        headers=_headers(auth_context),
    )
    assert kb_resp.status_code == 201
    kb_id = kb_resp.json()["id"]

    # 2. Ingest a document.
    doc_resp = await client.post(
        f"/v1/knowledge/knowledge-bases/{kb_id}/documents",
        json={
            "title": "佣金政策",
            "content": "# 佣金政策\n\n新商家首月免佣金，第二月起按 3% 收取佣金。",
        },
        headers=_headers(auth_context),
    )
    assert doc_resp.status_code == 201
    assert doc_resp.json()["chunk_count"] >= 1
    assert doc_resp.json()["status"] == "published"

    # 3. Ask a question -> retrieval finds evidence -> not the abstain answer.
    conv = await client.post("/v1/conversations", json={}, headers=_headers(auth_context))
    conv_id = conv.json()["id"]
    ask = await client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"content": "佣金政策是什么", "stream": False},
        headers=_headers(auth_context, "idem-e2e-ingest-01"),
    )
    assert ask.status_code == 200
    answer = ask.json()["assistant_message"]["content"]
    assert "没有检索到" not in answer  # evidence was found, so not the abstain reply


async def test_ingested_chunks_are_tenant_scoped(client: AsyncClient) -> None:
    owner = AuthContext(tenant_id=uuid4(), user_id=uuid4())
    other = AuthContext(tenant_id=uuid4(), user_id=uuid4())

    kb = await client.post(
        "/v1/knowledge/knowledge-bases", json={"name": "kb"}, headers=_headers(owner)
    )
    kb_id = kb.json()["id"]
    await client.post(
        f"/v1/knowledge/knowledge-bases/{kb_id}/documents",
        json={"title": "机密政策", "content": "# 机密\n\n仅本租户可见的政策内容。"},
        headers=_headers(owner),
    )

    # Another tenant asks the same question -> no evidence -> abstain.
    conv = await client.post("/v1/conversations", json={}, headers=_headers(other))
    conv_id = conv.json()["id"]
    ask = await client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"content": "机密政策内容是什么", "stream": False},
        headers=_headers(other, "idem-e2e-iso-01"),
    )
    answer = ask.json()["assistant_message"]["content"]
    assert "没有检索到" in answer  # cross-tenant leakage prevented
