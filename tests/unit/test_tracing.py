from __future__ import annotations

from uuid import uuid4

from sales_assistant.infrastructure.observability.tracing import (
    LangfuseTraceHandlerFactory,
    NullTraceHandlerFactory,
    build_trace_handler_factory,
)
from sales_assistant.settings import AppEnvironment, Settings


def _settings(
    *,
    tracing_enabled: bool = False,
    langfuse_public_key: str | None = None,
    langfuse_secret_key: str | None = None,
) -> Settings:
    return Settings(
        app_env=AppEnvironment.TEST,
        model_provider="mock",
        embedding_provider="mock",
        embedding_base_url=None,
        embedding_api_key=None,
        llm_base_url=None,
        llm_api_key=None,
        tracing_enabled=tracing_enabled,
        langfuse_public_key=langfuse_public_key,
        langfuse_secret_key=langfuse_secret_key,
    )


def test_null_factory_returns_no_handlers() -> None:
    factory = NullTraceHandlerFactory()
    handlers = factory.for_run(
        run_id=uuid4(), tenant_id=uuid4(), user_id=uuid4(), conversation_id=uuid4()
    )
    assert handlers == []


def test_build_factory_disabled_by_default() -> None:
    factory = build_trace_handler_factory(_settings())
    assert isinstance(factory, NullTraceHandlerFactory)


def test_build_factory_enabled_but_missing_keys_is_noop() -> None:
    factory = build_trace_handler_factory(_settings(tracing_enabled=True))
    assert isinstance(factory, NullTraceHandlerFactory)


def test_build_factory_enabled_with_keys() -> None:
    factory = build_trace_handler_factory(
        _settings(
            tracing_enabled=True,
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        )
    )
    assert isinstance(factory, LangfuseTraceHandlerFactory)
