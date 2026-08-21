"""Knowledge base, document, document version tables (T-201).

Revision ID: 20260820_0003
Revises: 20260813_0002
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from sales_assistant.infrastructure.mysql.types import UuidBinary

revision: str = "20260820_0003"
down_revision: str | None = "20260813_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MYSQL_ARGS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def _dt() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "knowledge_bases",
        sa.Column("id", UuidBinary(), nullable=False),
        sa.Column("tenant_id", UuidBinary(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("created_at", _dt(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        **_MYSQL_ARGS,
    )
    op.create_index("ix_kb_tenant", "knowledge_bases", ["tenant_id"])

    op.create_table(
        "documents",
        sa.Column("id", UuidBinary(), nullable=False),
        sa.Column("tenant_id", UuidBinary(), nullable=False),
        sa.Column("knowledge_base_id", UuidBinary(), nullable=False),
        sa.Column("logical_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("created_at", _dt(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        **_MYSQL_ARGS,
    )
    op.create_index("ix_doc_tenant_kb", "documents", ["tenant_id", "knowledge_base_id"])

    op.create_table(
        "document_versions",
        sa.Column("id", UuidBinary(), nullable=False),
        sa.Column("tenant_id", UuidBinary(), nullable=False),
        sa.Column("document_id", UuidBinary(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("parser_version", sa.String(length=32), nullable=False),
        sa.Column("chunker_version", sa.String(length=32), nullable=False),
        sa.Column("embedding_model", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("security_level", sa.String(length=16), nullable=False, server_default="normal"),
        sa.Column("acl_tokens", sa.JSON(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="processing"),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("created_at", _dt(), nullable=False),
        sa.Column("updated_at", _dt(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "version", name="uk_docver"),
        **_MYSQL_ARGS,
    )
    op.create_index("ix_docver_tenant_status", "document_versions", ["tenant_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_docver_tenant_status", table_name="document_versions")
    op.drop_table("document_versions")
    op.drop_index("ix_doc_tenant_kb", table_name="documents")
    op.drop_table("documents")
    op.drop_index("ix_kb_tenant", table_name="knowledge_bases")
    op.drop_table("knowledge_bases")
