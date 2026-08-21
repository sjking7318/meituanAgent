from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGBLOB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sales_assistant.infrastructure.mysql.types import UuidBinary


class Base(DeclarativeBase):
    pass


class TenantRecord(Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="active")
    retention_days: Mapped[int] = mapped_column(Integer, default=365)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UserRecord(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "external_subject", name="uk_users_tenant_subject"),
        Index("ix_users_tenant", "tenant_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary())
    external_subject: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="active")
    ltm_enabled: Mapped[bool] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConversationRecord(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conv_tenant_owner_updated", "tenant_id", "owner_id", "updated_at"),)

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), index=True)
    owner_id: Mapped[UUID] = mapped_column(UuidBinary(), index=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MessageRecord(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uk_msg_conv_seq"),
        Index("ix_msg_tenant_conv_seq", "tenant_id", "conversation_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), index=True)
    conversation_id: Mapped[UUID] = mapped_column(
        UuidBinary(),
        ForeignKey("conversations.id", ondelete="CASCADE"),
    )
    role: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    sequence: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConversationSummaryRecord(Base):
    """Versioned rolling summary of a conversation (memory-design.md 3, 5)."""

    __tablename__ = "conversation_summaries"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "source_version", name="uk_convsum_conv_srcver"
        ),
        Index("ix_convsum_tenant_conv", "tenant_id", "conversation_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), index=True)
    conversation_id: Mapped[UUID] = mapped_column(
        UuidBinary(),
        ForeignKey("conversations.id", ondelete="CASCADE"),
    )
    summary: Mapped[str] = mapped_column(Text)
    covered_through_sequence: Mapped[int] = mapped_column(Integer)
    source_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentRunRecord(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uk_run_tenant_idem"),
        Index("ix_run_conv_created", "conversation_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), index=True)
    conversation_id: Mapped[UUID] = mapped_column(
        UuidBinary(),
        ForeignKey("conversations.id", ondelete="CASCADE"),
    )
    user_id: Mapped[UUID] = mapped_column(UuidBinary())
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    expected_conversation_version: Mapped[int] = mapped_column(Integer)
    fencing_token: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    user_message_id: Mapped[UUID | None] = mapped_column(UuidBinary(), nullable=True)
    assistant_message_id: Mapped[UUID | None] = mapped_column(UuidBinary(), nullable=True)
    route_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    budgets_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    versions_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RunEventRecord(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uk_revent_run_seq"),
        Index("ix_revent_tenant_run_seq", "tenant_id", "run_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), index=True)
    run_id: Mapped[UUID] = mapped_column(
        UuidBinary(),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
    )
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OutboxEventRecord(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_unpublished", "published_at", "created_at"),)

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary())
    aggregate: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[UUID] = mapped_column(UuidBinary())
    topic: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LangGraphCheckpointRecord(Base):
    """LangGraph checkpoint storage (ADR-0004)."""

    __tablename__ = "lg_checkpoints"
    __table_args__ = (Index("ix_lgc_tenant_thread", "tenant_id", "thread_id"),)

    thread_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(String(128), primary_key=True, default="")
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary())
    parent_checkpoint_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checkpoint_type: Mapped[str] = mapped_column(String(32))
    checkpoint_blob: Mapped[bytes] = mapped_column(LargeBinary().with_variant(LONGBLOB, "mysql"))
    metadata_blob: Mapped[bytes] = mapped_column(LargeBinary().with_variant(LONGBLOB, "mysql"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LangGraphCheckpointWriteRecord(Base):
    """LangGraph intermediate channel writes (ADR-0004)."""

    __tablename__ = "lg_checkpoint_writes"

    thread_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(String(128), primary_key=True, default="")
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary())
    channel: Mapped[str] = mapped_column(String(128))
    write_type: Mapped[str] = mapped_column(String(32))
    write_blob: Mapped[bytes] = mapped_column(LargeBinary().with_variant(LONGBLOB, "mysql"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeBaseRecord(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (Index("ix_kb_tenant", "tenant_id"),)

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary())
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DocumentRecord(Base):
    __tablename__ = "documents"
    __table_args__ = (Index("ix_doc_tenant_kb", "tenant_id", "knowledge_base_id"),)

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary())
    knowledge_base_id: Mapped[UUID] = mapped_column(UuidBinary())
    logical_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DocumentVersionRecord(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version", name="uk_docver"),
        Index("ix_docver_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary())
    document_id: Mapped[UUID] = mapped_column(UuidBinary())
    version: Mapped[int] = mapped_column(Integer)
    source_hash: Mapped[str] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(32))
    chunker_version: Mapped[str] = mapped_column(String(32))
    embedding_model: Mapped[str] = mapped_column(String(64))
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    security_level: Mapped[str] = mapped_column(String(16), default="normal")
    acl_tokens: Mapped[dict[str, Any]] = mapped_column(JSON)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="processing")
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
