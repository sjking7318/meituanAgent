"""Conversation rolling-summary table (memory-design.md 3, 5).

Revision ID: 20260820_0005
Revises: 20260820_0004
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from sales_assistant.infrastructure.mysql.types import UuidBinary

revision: str = "20260820_0005"
down_revision: str | None = "20260820_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MYSQL_ARGS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def _dt() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "conversation_summaries",
        sa.Column("id", UuidBinary(), nullable=False),
        sa.Column("tenant_id", UuidBinary(), nullable=False),
        sa.Column("conversation_id", UuidBinary(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("covered_through_sequence", sa.Integer(), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("created_at", _dt(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id", "source_version", name="uk_convsum_conv_srcver"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        **_MYSQL_ARGS,
    )
    op.create_index(
        "ix_convsum_tenant_conv",
        "conversation_summaries",
        ["tenant_id", "conversation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_convsum_tenant_conv", table_name="conversation_summaries")
    op.drop_table("conversation_summaries")
