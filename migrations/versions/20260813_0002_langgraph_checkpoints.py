"""LangGraph checkpoint tables (ADR-0004).

Revision ID: 20260813_0002
Revises: 20260811_0001
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

from sales_assistant.infrastructure.mysql.types import UuidBinary

revision: str = "20260813_0002"
down_revision: str | None = "20260811_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MYSQL_ARGS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def _dt() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def _blob() -> sa.types.TypeEngine:
    return sa.LargeBinary().with_variant(mysql.LONGBLOB(), "mysql")


def upgrade() -> None:
    op.create_table(
        "lg_checkpoints",
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("checkpoint_ns", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("checkpoint_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", UuidBinary(), nullable=False),
        sa.Column("parent_checkpoint_id", sa.String(length=64), nullable=True),
        sa.Column("checkpoint_type", sa.String(length=32), nullable=False),
        sa.Column("checkpoint_blob", _blob(), nullable=False),
        sa.Column("metadata_blob", _blob(), nullable=False),
        sa.Column("created_at", _dt(), nullable=False),
        sa.PrimaryKeyConstraint("thread_id", "checkpoint_ns", "checkpoint_id"),
        **_MYSQL_ARGS,
    )
    op.create_index("ix_lgc_tenant_thread", "lg_checkpoints", ["tenant_id", "thread_id"])

    op.create_table(
        "lg_checkpoint_writes",
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("checkpoint_ns", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("checkpoint_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("tenant_id", UuidBinary(), nullable=False),
        sa.Column("channel", sa.String(length=128), nullable=False),
        sa.Column("write_type", sa.String(length=32), nullable=False),
        sa.Column("write_blob", _blob(), nullable=False),
        sa.Column("created_at", _dt(), nullable=False),
        sa.PrimaryKeyConstraint("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"),
        **_MYSQL_ARGS,
    )


def downgrade() -> None:
    op.drop_table("lg_checkpoint_writes")
    op.drop_index("ix_lgc_tenant_thread", table_name="lg_checkpoints")
    op.drop_table("lg_checkpoints")
