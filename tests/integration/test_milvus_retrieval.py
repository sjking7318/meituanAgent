"""Integration test for Milvus dual-route recall + ACL filtering (T-207/T-208).

Requires a running Milvus (``make up``). Skipped automatically when the cluster
is unreachable so ``make test`` stays hermetic. Uses a unique temporary
collection per run and drops it afterwards.

This exercises the real schema (dense HNSW + sparse BM25) and the scalar
pre-filter expression produced by ``build_filter_expr`` against a live Milvus.
The async client wrapper is covered separately by in-memory unit tests, since
``AsyncMilvusClient`` binds its grpc channel to its own loop.

Run with: ``uv run pytest tests/integration -m integration``
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from sales_assistant.domain import RetrievalFilters
from sales_assistant.infrastructure.milvus import schema as fields
from sales_assistant.infrastructure.milvus.retriever import build_filter_expr
from sales_assistant.settings import get_settings

pytestmark = pytest.mark.integration

_DIM = 8


def _vector(seed: float) -> list[float]:
    return [seed / (i + 1) for i in range(_DIM)]


@pytest.fixture
def milvus_collection() -> Iterator[tuple[Any, str]]:
    pytest.importorskip("pymilvus")
    from pymilvus import MilvusClient

    uri = get_settings().milvus_uri
    client = MilvusClient(uri=uri)
    try:
        client.list_collections()
    except Exception:
        pytest.skip("Milvus not reachable; run `make up`")

    name = f"kc_test_{uuid.uuid4().hex[:8]}"
    schema = fields.build_schema(client, dense_dim=_DIM)
    index_params = fields.build_index_params(client)
    client.create_collection(collection_name=name, schema=schema, index_params=index_params)
    try:
        yield client, name
    finally:
        client.drop_collection(name)


def _row(
    pk: str, tenant: str, text: str, seed: float, *, acl: list[str], level: str = "normal"
) -> dict[str, Any]:
    now = int(time.time() * 1000)
    return {
        fields.FIELD_PK: pk,
        fields.FIELD_TEXT: text,
        fields.FIELD_DENSE: _vector(seed),
        fields.FIELD_PARENT: "p1",
        fields.FIELD_DOC_VERSION: "dv1",
        fields.FIELD_TENANT: tenant,
        fields.FIELD_KB: "kb1",
        fields.FIELD_ACL: acl,
        fields.FIELD_STATUS: "published",
        fields.FIELD_SECURITY: level,
        fields.FIELD_PRODUCT: "team",
        fields.FIELD_REGION: "east",
        fields.FIELD_EFFECTIVE: now - 1000,
        fields.FIELD_EXPIRES: 0,
        fields.FIELD_TITLE: "政策",
        fields.FIELD_SECTION: "第一章",
        fields.FIELD_PAGE: 1,
    }


def test_dual_route_recall_and_acl(milvus_collection: tuple[Any, str]) -> None:
    client, name = milvus_collection
    tenant_a = str(uuid.uuid4())
    tenant_b = str(uuid.uuid4())

    client.insert(
        collection_name=name,
        data=[
            _row("a1", tenant_a, "新商家首月免佣金政策说明", 1.0, acl=["team-x"]),
            _row("a2", tenant_a, "老商家续约优惠政策", 2.0, acl=["team-y"]),
            _row("b1", tenant_b, "另一个租户的政策", 3.0, acl=["team-x"]),
        ],
    )
    client.flush(collection_name=name)
    client.load_collection(collection_name=name)
    # BM25 sparse index needs the segment flushed + built before it is queryable.
    time.sleep(2)

    expr = build_filter_expr(
        tenant_id=uuid.UUID(tenant_a),
        acl_tokens=["team-x"],
        filters=RetrievalFilters(),
    )

    # BM25 keyword recall (sparse), tenant + ACL pre-filtered.
    bm25 = client.search(
        collection_name=name,
        data=["免佣金政策"],
        anns_field=fields.FIELD_SPARSE,
        filter=expr,
        limit=10,
        output_fields=[fields.FIELD_DOC_VERSION],
        search_params={"metric_type": "BM25"},
    )
    bm25_ids = {hit.get("id", hit.get("pk")) for hit in bm25[0]}
    assert "a1" in bm25_ids  # keyword + tenant + acl match
    assert "b1" not in bm25_ids  # different tenant filtered out
    assert "a2" not in bm25_ids  # acl token mismatch filtered out

    # Dense vector recall, same pre-filter.
    dense = client.search(
        collection_name=name,
        data=[_vector(1.0)],
        anns_field=fields.FIELD_DENSE,
        filter=expr,
        limit=10,
        output_fields=[fields.FIELD_DOC_VERSION],
        search_params={"metric_type": "COSINE"},
    )
    dense_ids = {hit.get("id", hit.get("pk")) for hit in dense[0]}
    assert dense_ids <= {"a1"}  # only the tenant+acl authorised chunk
