from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

import structlog

from sales_assistant.settings import Settings

logger = structlog.get_logger()


class TraceHandlerFactory(Protocol):
    """Builds LangChain callback handlers scoped to a single agent run.

    Returning an empty list disables tracing (no-op). Kept behind a Protocol so
    the agent runtime never imports the Langfuse SDK directly (hexagonal edge).
    """

    def for_run(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
        conversation_id: UUID,
    ) -> list[Any]: ...


class NullTraceHandlerFactory:
    """No tracing: every run gets an empty callback list."""

    def for_run(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
        conversation_id: UUID,
    ) -> list[Any]:
        return []


class LangfuseTraceHandlerFactory:
    """Creates a per-run Langfuse callback handler (Langfuse SDK v2).

    The handler is attached to the LangGraph ``RunnableConfig.callbacks`` so the
    whole graph invocation becomes one trace and every node (supervisor,
    retrieve, knowledge_qa/chitchat/clarify, synthesize) plus each LLM call
    nests as a child span automatically.
    """

    def __init__(self, *, public_key: str, secret_key: str, host: str) -> None:
        self._public_key = public_key
        self._secret_key = secret_key
        self._host = host

    def for_run(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
        conversation_id: UUID,
    ) -> list[Any]:
        try:
            from langfuse.callback import CallbackHandler
        except Exception:
            logger.warning("langfuse_sdk_missing_tracing_disabled")
            return []
        try:
            handler = CallbackHandler(
                public_key=self._public_key,
                secret_key=self._secret_key,
                host=self._host,
                session_id=str(conversation_id),
                user_id=str(user_id),
                trace_name="sales-assistant-run",
                metadata={
                    "run_id": str(run_id),
                    "tenant_id": str(tenant_id),
                    "conversation_id": str(conversation_id),
                },
                tags=["sales-assistant"],
            )
        except Exception:
            logger.warning("langfuse_handler_init_failed_tracing_disabled")
            return []
        return [handler]


def build_trace_handler_factory(settings: Settings) -> TraceHandlerFactory:
    if not settings.tracing_enabled:
        return NullTraceHandlerFactory()
    if settings.langfuse_public_key is None or settings.langfuse_secret_key is None:
        logger.warning("tracing_enabled_but_keys_missing_tracing_disabled")
        return NullTraceHandlerFactory()
    return LangfuseTraceHandlerFactory(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key.get_secret_value(),
        host=settings.langfuse_host,
    )
