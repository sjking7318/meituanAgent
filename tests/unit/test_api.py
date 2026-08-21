from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

from sales_assistant.application.conversation_service import ConversationService
from sales_assistant.domain import AuthContext, IdempotencyConflictError
from sales_assistant.main import Container

pytestmark = pytest.mark.asyncio


def _headers(context: AuthContext, idem: str | None = None) -> dict[str, str]:
    headers = {
        "X-Tenant-ID": str(context.tenant_id),
        "X-User-ID": str(context.user_id),
    }
    if idem is not None:
        headers["Idempotency-Key"] = idem
    return headers


async def test_health_endpoints(client: AsyncClient) -> None:
    assert (await client.get("/health/live")).status_code == 200
    assert (await client.get("/health/ready")).status_code == 200


async def test_create_conversation_and_send_message(
    client: AsyncClient, auth_context: AuthContext
) -> None:
    resp = await client.post(
        "/v1/conversations", json={"title": "hi"}, headers=_headers(auth_context)
    )
    assert resp.status_code == 201
    conv_id = resp.json()["id"]

    resp = await client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"content": "产品政策是什么", "stream": False},
        headers=_headers(auth_context, "idem-key-0001"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["replayed"] is False
    assert body["assistant_message"] is not None
    assert body["run"]["status"] == "succeeded"


async def test_idempotent_replay_returns_same_run(
    client: AsyncClient, auth_context: AuthContext
) -> None:
    resp = await client.post("/v1/conversations", json={}, headers=_headers(auth_context))
    conv_id = resp.json()["id"]
    headers = _headers(auth_context, "idem-key-0002")
    payload = {"content": "重复请求", "stream": False}

    first = await client.post(
        f"/v1/conversations/{conv_id}/messages", json=payload, headers=headers
    )
    second = await client.post(
        f"/v1/conversations/{conv_id}/messages", json=payload, headers=headers
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert first.json()["run"]["id"] == second.json()["run"]["id"]


async def test_idempotency_conflict_different_content(
    container: Container, auth_context: AuthContext
) -> None:
    service: ConversationService = container.conversation_service
    conv = await service.create_conversation(auth_context, title=None)
    await service.send_message(
        auth_context, conv.id, content="first", idempotency_key="idem-key-0003"
    )
    with pytest.raises(IdempotencyConflictError):
        await service.send_message(
            auth_context, conv.id, content="different", idempotency_key="idem-key-0003"
        )


async def test_cross_tenant_conversation_hidden(
    client: AsyncClient, auth_context: AuthContext
) -> None:
    resp = await client.post("/v1/conversations", json={}, headers=_headers(auth_context))
    conv_id = resp.json()["id"]

    other = AuthContext(tenant_id=uuid4(), user_id=uuid4())
    resp = await client.get(f"/v1/conversations/{conv_id}", headers=_headers(other))
    assert resp.status_code == 404


async def test_missing_auth_headers_rejected(client: AsyncClient) -> None:
    resp = await client.post("/v1/conversations", json={})
    assert resp.status_code == 401
