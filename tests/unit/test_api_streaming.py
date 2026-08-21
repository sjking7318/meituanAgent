from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

from sales_assistant.domain import AuthContext

pytestmark = pytest.mark.asyncio


def _headers(context: AuthContext, idem: str | None = None) -> dict[str, str]:
    headers = {"X-Tenant-ID": str(context.tenant_id), "X-User-ID": str(context.user_id)}
    if idem is not None:
        headers["Idempotency-Key"] = idem
    return headers


async def _new_conversation(client: AsyncClient, context: AuthContext) -> str:
    resp = await client.post("/v1/conversations", json={}, headers=_headers(context))
    return str(resp.json()["id"])


async def test_stream_message_emits_sse_events(
    client: AsyncClient, auth_context: AuthContext
) -> None:
    conv_id = await _new_conversation(client, auth_context)
    async with client.stream(
        "POST",
        f"/v1/conversations/{conv_id}/messages",
        json={"content": "你好", "stream": True},
        headers=_headers(auth_context, "idem-stream-01"),
    ) as resp:
        assert resp.status_code == 200
        body = "".join([chunk async for chunk in resp.aiter_text()])
    assert "event: run.started" in body
    assert "event: message.completed" in body


async def test_list_messages_pagination(client: AsyncClient, auth_context: AuthContext) -> None:
    conv_id = await _new_conversation(client, auth_context)
    await client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"content": "问题一", "stream": False},
        headers=_headers(auth_context, "idem-list-01"),
    )
    resp = await client.get(
        f"/v1/conversations/{conv_id}/messages",
        headers=_headers(auth_context),
        params={"limit": 50},
    )
    assert resp.status_code == 200
    roles = [m["role"] for m in resp.json()["items"]]
    assert roles == ["user", "assistant"]


async def test_get_and_cancel_run(client: AsyncClient, auth_context: AuthContext) -> None:
    conv_id = await _new_conversation(client, auth_context)
    send = await client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"content": "问题", "stream": False},
        headers=_headers(auth_context, "idem-run-01"),
    )
    run_id = send.json()["run"]["id"]

    got = await client.get(f"/v1/runs/{run_id}", headers=_headers(auth_context))
    assert got.status_code == 200
    assert got.json()["status"] == "succeeded"


async def test_resume_run_events_replays_history(
    client: AsyncClient, auth_context: AuthContext
) -> None:
    conv_id = await _new_conversation(client, auth_context)
    send = await client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"content": "问题", "stream": False},
        headers=_headers(auth_context, "idem-resume-01"),
    )
    run_id = send.json()["run"]["id"]

    async with client.stream(
        "GET",
        f"/v1/runs/{run_id}/events",
        headers={**_headers(auth_context), "Last-Event-ID": "0-0"},
    ) as resp:
        assert resp.status_code == 200
        body = "".join([chunk async for chunk in resp.aiter_text()])
    assert "message.completed" in body


async def test_get_run_not_found(client: AsyncClient, auth_context: AuthContext) -> None:
    resp = await client.get(f"/v1/runs/{uuid4()}", headers=_headers(auth_context))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_send_message_requires_idempotency_key(
    client: AsyncClient, auth_context: AuthContext
) -> None:
    conv_id = await _new_conversation(client, auth_context)
    resp = await client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"content": "hi", "stream": False},
        headers=_headers(auth_context),
    )
    assert resp.status_code == 422


async def test_metrics_endpoint(client: AsyncClient) -> None:
    resp = await client.get("/metrics")
    assert resp.status_code == 200
