from __future__ import annotations

from pymilvus import DataType, Function, FunctionType, MilvusClient

# Collection + field names (data-model.md 4.1).
COLLECTION = "knowledge_chunks"
ACTIVE_ALIAS = "knowledge_active"

FIELD_PK = "pk"
FIELD_DENSE = "dense"
FIELD_SPARSE = "sparse"
FIELD_TEXT = "text"
FIELD_PARENT = "parent_id"
FIELD_DOC_VERSION = "document_version_id"
FIELD_TENANT = "tenant_id"
FIELD_KB = "knowledge_base_id"
FIELD_ACL = "acl_tokens"
FIELD_STATUS = "status"
FIELD_SECURITY = "security_level"
FIELD_PRODUCT = "product"
FIELD_REGION = "region"
FIELD_EFFECTIVE = "effective_at"
FIELD_EXPIRES = "expires_at"
FIELD_TITLE = "title"
FIELD_SECTION = "section_path"
FIELD_PAGE = "page"

_BM25_FUNCTION = "text_bm25"


def build_schema(client: MilvusClient, *, dense_dim: int) -> object:
    """Schema for knowledge_chunks: dense HNSW + sparse BM25 (ADR-0002).

    The sparse vector is generated from ``text`` by Milvus' built-in BM25
    function; a Chinese-friendly analyzer tokenises the text.
    """
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field(FIELD_PK, DataType.VARCHAR, is_primary=True, max_length=256)
    schema.add_field(
        FIELD_TEXT,
        DataType.VARCHAR,
        max_length=8192,
        enable_analyzer=True,
        analyzer_params={"type": "chinese"},
    )
    schema.add_field(FIELD_DENSE, DataType.FLOAT_VECTOR, dim=dense_dim)
    schema.add_field(FIELD_SPARSE, DataType.SPARSE_FLOAT_VECTOR)
    schema.add_field(FIELD_PARENT, DataType.VARCHAR, max_length=256)
    schema.add_field(FIELD_DOC_VERSION, DataType.VARCHAR, max_length=64)
    schema.add_field(FIELD_TENANT, DataType.VARCHAR, max_length=64)
    schema.add_field(FIELD_KB, DataType.VARCHAR, max_length=64)
    schema.add_field(
        FIELD_ACL, DataType.ARRAY, element_type=DataType.VARCHAR, max_capacity=64, max_length=128
    )
    schema.add_field(FIELD_STATUS, DataType.VARCHAR, max_length=16)
    schema.add_field(FIELD_SECURITY, DataType.VARCHAR, max_length=16)
    schema.add_field(FIELD_PRODUCT, DataType.VARCHAR, max_length=64)
    schema.add_field(FIELD_REGION, DataType.VARCHAR, max_length=64)
    schema.add_field(FIELD_EFFECTIVE, DataType.INT64)
    schema.add_field(FIELD_EXPIRES, DataType.INT64)
    schema.add_field(FIELD_TITLE, DataType.VARCHAR, max_length=512)
    schema.add_field(FIELD_SECTION, DataType.VARCHAR, max_length=512)
    schema.add_field(FIELD_PAGE, DataType.INT64)

    schema.add_function(
        Function(
            name=_BM25_FUNCTION,
            function_type=FunctionType.BM25,
            input_field_names=[FIELD_TEXT],
            output_field_names=[FIELD_SPARSE],
        )
    )
    return schema


def build_index_params(client: MilvusClient) -> object:
    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name=FIELD_DENSE,
        index_type="HNSW",
        metric_type="COSINE",
        params={"M": 16, "efConstruction": 200},
    )
    index_params.add_index(
        field_name=FIELD_SPARSE,
        index_type="SPARSE_INVERTED_INDEX",
        metric_type="BM25",
    )
    return index_params
