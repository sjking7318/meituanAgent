"""Initial schema: conversations, messages, runs, events, outbox.

Revision ID: 20260811_0001
Revises:
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from sales_assistant.infrastructure.mysql.types import UuidBinary

revision: str = "20260811_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MYSQL_ARGS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def _dt() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", UuidBinary(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="365"),
        sa.Column("created_at", _dt(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        **_MYSQL_ARGS,
    )
    op.create_table(
        "users",
        sa.Column("id", UuidBinary(), nullable=False),
        sa.Column("tenant_id", UuidBinary(), nullable=False),
        sa.Column("external_subject", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("ltm_enabled", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", _dt(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "external_subject", name="uk_users_tenant_subject"),
        **_MYSQL_ARGS,
    )
    op.create_index("ix_users_tenant", "users", ["tenant_id"])

    op.create_table(
        "conversations",
        sa.Column("id", UuidBinary(), nullable=False),
        sa.Column("tenant_id", UuidBinary(), nullable=False),
        sa.Column("owner_id", UuidBinary(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", _dt(), nullable=False),
        sa.Column("updated_at", _dt(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        **_MYSQL_ARGS,
    )
    op.create_index("ix_conversations_tenant_id", "conversations", ["tenant_id"])
    op.create_index("ix_conversations_owner_id", "conversations", ["owner_id"])
    op.create_index(
        "ix_conv_tenant_owner_updated",
        "conversations",
        ["tenant_id", "owner_id", "updated_at"],
    )

    op.create_table(
        "messages",
        sa.Column("id", UuidBinary(), nullable=False),
        sa.Column("tenant_id", UuidBinary(), nullable=False),
        sa.Column("conversation_id", UuidBinary(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", _dt(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "sequence", name="uk_msg_conv_seq"),
        **_MYSQL_ARGS,
    )
    op.create_index("ix_messages_tenant_id", "messages", ["tenant_id"])
    op.create_index(
        "ix_msg_tenant_conv_seq",
        "messages",
        ["tenant_id", "conversation_id", "sequence"],
    )

    op.create_table(
        "agent_runs",
        sa.Column("id", UuidBinary(), nullable=False),
        sa.Column("tenant_id", UuidBinary(), nullable=False),
        sa.Column("conversation_id", UuidBinary(), nullable=False),
        sa.Column("user_id", UuidBinary(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("expected_conversation_version", sa.Integer(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("user_message_id", UuidBinary(), nullable=True),
        sa.Column("assistant_message_id", UuidBinary(), nullable=True),
        sa.Column("route_json", sa.JSON(), nullable=True),
        sa.Column("budgets_json", sa.JSON(), nullable=True),
        sa.Column("versions_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("created_at", _dt(), nullable=False),
        sa.Column("updated_at", _dt(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uk_run_tenant_idem"),
        **_MYSQL_ARGS,
    )
    op.create_index("ix_agent_runs_tenant_id", "agent_runs", ["tenant_id"])
    op.create_index("ix_run_conv_created", "agent_runs", ["conversation_id", "created_at"])

    op.create_table(
        "run_events",
        sa.Column("id", UuidBinary(), nullable=False),
        sa.Column("tenant_id", UuidBinary(), nullable=False),
        sa.Column("run_id", UuidBinary(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", _dt(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence", name="uk_revent_run_seq"),
        **_MYSQL_ARGS,
    )
    op.create_index("ix_run_events_tenant_id", "run_events", ["tenant_id"])
    op.create_index(
        "ix_revent_tenant_run_seq",
        "run_events",
        ["tenant_id", "run_id", "sequence"],
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", UuidBinary(), nullable=False),
        sa.Column("tenant_id", UuidBinary(), nullable=False),
        sa.Column("aggregate", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", UuidBinary(), nullable=False),
        sa.Column("topic", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("published_at", _dt(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", _dt(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        **_MYSQL_ARGS,
    )
    op.create_index("ix_outbox_unpublished", "outbox_events", ["published_at", "created_at"])


def downgrade() -> None:
    op.drop_table("outbox_events")
    op.drop_index("ix_revent_tenant_run_seq", table_name="run_events")
    op.drop_index("ix_run_events_tenant_id", table_name="run_events")
    op.drop_table("run_events")
    op.drop_index("ix_run_conv_created", table_name="agent_runs")
    op.drop_index("ix_agent_runs_tenant_id", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("ix_msg_tenant_conv_seq", table_name="messages")
    op.drop_index("ix_messages_tenant_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_conv_tenant_owner_updated", table_name="conversations")
    op.drop_index("ix_conversations_owner_id", table_name="conversations")
    op.drop_index("ix_conversations_tenant_id", table_name="conversations")
    op.drop_table("conversations")
    op.drop_index("ix_users_tenant", table_name="users")
    op.drop_table("users")
    op.drop_table("tenants")
