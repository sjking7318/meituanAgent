from __future__ import annotations

import pytest

from sales_assistant.settings import (
    AppEnvironment,
    AuthMode,
    EmbeddingProvider,
    ModelProvider,
    Settings,
)

# Explicit kwargs neutralise any local .env (init args have highest priority in
# pydantic-settings), keeping these guard tests hermetic.


def test_production_forbids_disabled_auth() -> None:
    with pytest.raises(ValueError, match="AUTH_MODE=disabled is forbidden"):
        Settings(
            app_env=AppEnvironment.PRODUCTION,
            auth_mode=AuthMode.DISABLED,
            model_provider=ModelProvider.MOCK,
            embedding_provider=EmbeddingProvider.MOCK,
            llm_base_url=None,
            llm_api_key=None,
            embedding_base_url=None,
            embedding_api_key=None,
        )


def test_production_forbids_mock_model() -> None:
    with pytest.raises(ValueError, match="MODEL_PROVIDER=mock is forbidden"):
        Settings(
            app_env=AppEnvironment.PRODUCTION,
            auth_mode=AuthMode.OIDC,
            oidc_issuer="https://issuer",
            oidc_audience="aud",
            oidc_jwks_url="https://jwks",
            model_provider=ModelProvider.MOCK,
            embedding_provider=EmbeddingProvider.MOCK,
            llm_base_url=None,
            llm_api_key=None,
            embedding_base_url=None,
            embedding_api_key=None,
        )


def test_oidc_requires_endpoints() -> None:
    with pytest.raises(ValueError, match="OIDC mode requires"):
        Settings(
            auth_mode=AuthMode.OIDC,
            model_provider=ModelProvider.MOCK,
            embedding_provider=EmbeddingProvider.MOCK,
            llm_base_url=None,
            llm_api_key=None,
            embedding_base_url=None,
            embedding_api_key=None,
        )


def test_openai_compatible_requires_credentials() -> None:
    with pytest.raises(ValueError, match="OpenAI-compatible model requires"):
        Settings(
            model_provider=ModelProvider.OPENAI_COMPATIBLE,
            embedding_provider=EmbeddingProvider.MOCK,
            llm_base_url=None,
            llm_api_key=None,
            embedding_base_url=None,
            embedding_api_key=None,
        )


def test_settings_is_frozen() -> None:
    settings = Settings(
        app_env=AppEnvironment.TEST,
        model_provider=ModelProvider.MOCK,
        embedding_provider=EmbeddingProvider.MOCK,
        llm_base_url=None,
        llm_api_key=None,
        embedding_base_url=None,
        embedding_api_key=None,
    )
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError on frozen
        settings.log_level = "DEBUG"  # type: ignore[misc]
