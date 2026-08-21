from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import InMemorySaver

from sales_assistant.agents.runtime.graph import AgentRuntime
from sales_assistant.api.auth import Authenticator
from sales_assistant.application.conversation_service import ConversationService
from sales_assistant.application.ingestion_service import IngestionService
from sales_assistant.application.memory_service import MemoryService
from sales_assistant.application.retrieval_service import RetrievalConfig, RetrievalService
from sales_assistant.domain import AuthContext
from sales_assistant.infrastructure.milvus.memory import (
    InMemoryKnowledgeIndexer,
    InMemoryRetriever,
)
from sales_assistant.infrastructure.model_gateway.embeddings import MockEmbedder, MockReranker
from sales_assistant.infrastructure.model_gateway.gateway import MockModelGateway
from sales_assistant.infrastructure.mysql.database import Database
from sales_assistant.infrastructure.mysql.repositories import SqlUnitOfWorkFactory
from sales_assistant.infrastructure.redis.event_stream import InMemoryRunEventStream
from sales_assistant.infrastructure.redis.lease import InMemoryLeaseManager
from sales_assistant.main import Container
from sales_assistant.settings import AppEnvironment, Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env=AppEnvironment.TEST,
        model_provider="mock",
        embedding_provider="mock",
        embedding_base_url=None,
        embedding_api_key=None,
        llm_base_url=None,
        llm_api_key=None,
    )


@pytest_asyncio.fixture
async def database() -> AsyncIterator[Database]:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.create_schema()
    try:
        yield db
    finally:
        await db.dispose()


@pytest_asyncio.fixture
async def container(settings: Settings, database: Database) -> AsyncIterator[Container]:
    lease_manager = InMemoryLeaseManager()
    event_stream = InMemoryRunEventStream()
    model_gateway = MockModelGateway()
    retriever = InMemoryRetriever()
    indexer = InMemoryKnowledgeIndexer(retriever)
    embedder = MockEmbedder()
    retrieval_service = RetrievalService(retriever, embedder, MockReranker(), RetrievalConfig())
    agent_runtime = AgentRuntime(model_gateway, retrieval_service, InMemorySaver())
    uow_factory = SqlUnitOfWorkFactory(database.session_factory)
    memory_service = MemoryService(
        uow_factory,
        model_gateway,
        recent_turns=settings.stm_recent_turns,
        summary_trigger_turns=settings.stm_summary_trigger_turns,
    )
    service = ConversationService(
        uow_factory, agent_runtime, lease_manager, event_stream, memory_service
    )
    ingestion_service = IngestionService(
        database.session_factory, embedder, indexer, embedding_model="mock-embedding"
    )
    yield Container(
        settings=settings,
        database=database,
        authenticator=Authenticator(settings),
        conversation_service=service,
        ingestion_service=ingestion_service,
        lease_manager=lease_manager,
        event_stream=event_stream,
        model_gateway=model_gateway,
        retriever=retriever,
        indexer=indexer,
        agent_runtime=agent_runtime,
    )
    await lease_manager.close()
    await event_stream.close()


@pytest.fixture
def auth_context() -> AuthContext:
    return AuthContext(tenant_id=uuid4(), user_id=uuid4())


@pytest_asyncio.fixture
async def client(container: Container) -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from sales_assistant.api.errors import domain_error_handler
    from sales_assistant.api.knowledge_routes import router as knowledge_router
    from sales_assistant.api.routes import router
    from sales_assistant.domain import DomainError

    app = FastAPI()
    app.state.container = container
    app.include_router(router)
    app.include_router(knowledge_router)
    app.add_exception_handler(DomainError, domain_error_handler)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
