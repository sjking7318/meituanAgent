from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnvironment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class AuthMode(StrEnum):
    DISABLED = "disabled"
    OIDC = "oidc"


class ModelProvider(StrEnum):
    MOCK = "mock"
    OPENAI_COMPATIBLE = "openai_compatible"


class EmbeddingProvider(StrEnum):
    MOCK = "mock"
    OPENAI_COMPATIBLE = "openai_compatible"
    DASHSCOPE = "dashscope"


class Settings(BaseSettings):
    """Single-layer configuration loaded from environment (ADR-0005).

    All configuration is loaded and frozen at process start. Changes take
    effect via redeploy / rolling release. There is no runtime hot-reload.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        frozen=True,
    )

    app_name: str = "meituan-sales-assistant"
    app_env: AppEnvironment = AppEnvironment.LOCAL
    log_level: str = "INFO"
    host: str = "0.0.0.0"  # noqa: S104 - required for container networking
    port: int = Field(default=8000, ge=1, le=65535)

    # Infrastructure
    database_url: str = "mysql+asyncmy://sales:sales@localhost:3306/sales_assistant"
    redis_url: str = "redis://localhost:6379/0"
    milvus_uri: str = "http://localhost:19530"
    kafka_bootstrap: str = "localhost:9092"
    object_store_endpoint: str = "http://localhost:9000"

    # Auth
    auth_mode: AuthMode = AuthMode.DISABLED
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_tenant_claim: str = "tenant_id"
    oidc_user_id_claim: str = "user_id"

    # Model gateway
    model_provider: ModelProvider = ModelProvider.MOCK

    # LLM gateway (e.g. dashscope OpenAI-compatible endpoint)
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    model_name_router: str = "mock-router"
    model_name_synth: str = "mock-synth"

    # Embedding / Rerank gateway
    embedding_provider: EmbeddingProvider = EmbeddingProvider.MOCK
    embedding_base_url: str | None = None
    embedding_api_key: SecretStr | None = None
    embedding_model: str = "mock-embedding"
    rerank_model: str = "mock-reranker"

    model_timeout_seconds: float = Field(default=20.0, gt=0, le=120)

    # Retrieval params
    rrf_k: int = Field(default=60, ge=1)
    rrf_weight_dense: float = Field(default=1.0, ge=0)
    rrf_weight_bm25: float = Field(default=1.0, ge=0)
    retrieval_top_k: int = Field(default=50, ge=1, le=500)
    rerank_input: int = Field(default=40, ge=1, le=200)
    final_evidence: int = Field(default=8, ge=1, le=50)

    # Budgets
    max_model_calls: int = Field(default=8, ge=1, le=64)
    max_planner_tasks: int = Field(default=4, ge=1, le=16)
    max_reflections: int = Field(default=1, ge=0, le=5)
    wall_clock_seconds: float = Field(default=25.0, gt=0, le=300)

    # Feature flags
    feature_hyde: bool = True
    feature_reflection: bool = True
    feature_knowledge_loop: bool = True

    # Runtime
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    conversation_lease_seconds: int = Field(default=45, ge=10, le=300)
    sse_retention_seconds: int = Field(default=600, ge=60, le=3600)

    # Short-term memory (memory-design.md 3): sliding window + rolling summary.
    stm_recent_turns: int = Field(default=8, ge=1, le=64)
    stm_summary_trigger_turns: int = Field(default=12, ge=2, le=200)

    # Tracing (Langfuse). Disabled unless keys are provided; a missing SDK or
    # keys degrades to a no-op so the app runs without a tracing backend.
    tracing_enabled: bool = False
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None

    @property
    def is_production(self) -> bool:
        return self.app_env is AppEnvironment.PRODUCTION

    @model_validator(mode="after")
    def validate_environment_guards(self) -> Settings:
        if self.auth_mode is AuthMode.OIDC:
            required = (self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)
            if not all(required):
                raise ValueError("OIDC mode requires issuer, audience, and JWKS URL")

        if self.model_provider is ModelProvider.OPENAI_COMPATIBLE and (
            not self.llm_base_url or self.llm_api_key is None
        ):
            raise ValueError("OpenAI-compatible model requires LLM base URL and API key")

        if self.embedding_provider is not EmbeddingProvider.MOCK and (
            not self.embedding_base_url or self.embedding_api_key is None
        ):
            raise ValueError("Non-mock embedding provider requires embedding base URL and API key")

        if self.is_production:
            if self.auth_mode is AuthMode.DISABLED:
                raise ValueError("AUTH_MODE=disabled is forbidden in production")
            if self.model_provider is ModelProvider.MOCK:
                raise ValueError("MODEL_PROVIDER=mock is forbidden in production")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
