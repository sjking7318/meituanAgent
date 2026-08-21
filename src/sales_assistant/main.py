from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from sales_assistant.agents.runtime.checkpointer import MySQLCheckpointSaver
from sales_assistant.agents.runtime.graph import AgentRuntime
from sales_assistant.api.auth import Authenticator
from sales_assistant.api.errors import domain_error_handler
from sales_assistant.api.knowledge_routes import router as knowledge_router
from sales_assistant.api.routes import drain_background_tasks, router
from sales_assistant.application.conversation_service import ConversationService
from sales_assistant.application.ingestion_service import IngestionService
from sales_assistant.application.memory_service import MemoryService
from sales_assistant.application.retrieval_service import RetrievalConfig, RetrievalService
from sales_assistant.domain import (
    DomainError,
    KnowledgeIndexer,
    LeaseManager,
    ModelGateway,
    Retriever,
    RunEventStream,
)
from sales_assistant.infrastructure.milvus.factory import build_retrieval_backends
from sales_assistant.infrastructure.model_gateway.embeddings import build_embedder, build_reranker
from sales_assistant.infrastructure.model_gateway.gateway import build_model_gateway
from sales_assistant.infrastructure.mysql.database import Database
from sales_assistant.infrastructure.mysql.repositories import SqlUnitOfWorkFactory
from sales_assistant.infrastructure.observability.tracing import build_trace_handler_factory
from sales_assistant.infrastructure.redis.event_stream import build_run_event_stream
from sales_assistant.infrastructure.redis.lease import build_lease_manager
from sales_assistant.infrastructure.skills import build_skill_library
from sales_assistant.settings import Settings, get_settings


@dataclass
class Container:
    settings: Settings
    database: Database
    authenticator: Authenticator
    conversation_service: ConversationService
    ingestion_service: IngestionService
    lease_manager: LeaseManager
    event_stream: RunEventStream
    model_gateway: ModelGateway
    retriever: Retriever
    indexer: KnowledgeIndexer
    agent_runtime: AgentRuntime


def build_container(settings: Settings) -> Container:
    database = Database(settings.database_url)
    lease_manager = build_lease_manager(settings)
    event_stream = build_run_event_stream(settings)
    model_gateway = build_model_gateway(settings)
    embedder = build_embedder(settings)
    backends = build_retrieval_backends(settings)
    retrieval_service = RetrievalService(
        backends.retriever,
        embedder,
        build_reranker(settings),
        RetrievalConfig(
            rrf_k=settings.rrf_k,
            weight_dense=settings.rrf_weight_dense,
            weight_bm25=settings.rrf_weight_bm25,
            recall_top_k=settings.retrieval_top_k,
            rerank_input=settings.rerank_input,
            final_evidence=settings.final_evidence,
        ),
    )
    ingestion_service = IngestionService(
        database.session_factory,
        embedder,
        backends.indexer,
        embedding_model=settings.embedding_model,
    )
    unit_of_work_factory = SqlUnitOfWorkFactory(database.session_factory)
    checkpointer = MySQLCheckpointSaver(database.session_factory)
    trace_handler_factory = build_trace_handler_factory(settings)
    skill_library = build_skill_library()
    agent_runtime = AgentRuntime(
        model_gateway, retrieval_service, checkpointer, trace_handler_factory, skill_library
    )
    memory_service = MemoryService(
        unit_of_work_factory,
        model_gateway,
        recent_turns=settings.stm_recent_turns,
        summary_trigger_turns=settings.stm_summary_trigger_turns,
    )
    conversation_service = ConversationService(
        unit_of_work_factory,
        agent_runtime,
        lease_manager,
        event_stream,
        memory_service,
    )
    return Container(
        settings=settings,
        database=database,
        authenticator=Authenticator(settings),
        conversation_service=conversation_service,
        ingestion_service=ingestion_service,
        lease_manager=lease_manager,
        event_stream=event_stream,
        model_gateway=model_gateway,
        retriever=backends.retriever,
        indexer=backends.indexer,
        agent_runtime=agent_runtime,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    container: Container = app.state.container
    try:
        yield
    finally:
        await drain_background_tasks(container.settings.request_timeout_seconds)
        await container.lease_manager.close()
        await container.event_stream.close()
        await container.database.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.container = build_container(settings)
    # Permissive CORS: the bundled dev UI is same-origin, but this keeps a
    # standalone frontend (different port) working during local development.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    app.include_router(knowledge_router)
    app.add_exception_handler(DomainError, domain_error_handler)
    _mount_web_ui(app)
    return app


_WEB_DIR = Path(__file__).resolve().parents[2] / "web"


def _mount_web_ui(app: FastAPI) -> None:
    # Serve the single-page dev UI at the root when present (does not shadow
    # the /v1 and /health API routes registered above).
    if _WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "sales_assistant.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
