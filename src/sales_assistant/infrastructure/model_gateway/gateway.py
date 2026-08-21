from __future__ import annotations

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from sales_assistant.domain import ModelGateway, ModelRequest, ModelResponse
from sales_assistant.settings import ModelProvider, Settings


class MockModelGateway:
    """Deterministic gateway for tests and local runs without network."""

    def __init__(self, model_name: str = "mock-synth") -> None:
        self._model_name = model_name

    async def generate(self, request: ModelRequest) -> ModelResponse:
        content = (
            "【本地 Mock 回复】当前迭代未接入真实检索证据，"
            f"无法给出业务结论。收到问题：{request.user_prompt[:200]}"
        )
        return ModelResponse(
            content=content,
            model=self._model_name,
            input_tokens=len(request.user_prompt),
            output_tokens=len(content),
        )


class OpenAICompatibleModelGateway:
    """Connects to an external OpenAI-compatible gateway (ADR-0006)."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_name: str,
        timeout_seconds: float,
    ) -> None:
        self._model_name = model_name
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.2, max=2.0),
        retry=retry_if_exception_type(httpx.TransportError),
        reraise=True,
    )
    async def generate(self, request: ModelRequest) -> ModelResponse:
        messages = [{"role": "system", "content": request.system_prompt}]
        messages.extend(
            {"role": turn.role.value, "content": turn.content} for turn in request.history
        )
        messages.append({"role": "user", "content": request.user_prompt})

        response = await self._client.post(
            "/chat/completions",
            json={"model": self._model_name, "messages": messages},
        )
        response.raise_for_status()
        payload = response.json()
        choice = payload["choices"][0]["message"]["content"]
        usage = payload.get("usage", {})
        return ModelResponse(
            content=choice,
            model=payload.get("model", self._model_name),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
        )

    async def close(self) -> None:
        await self._client.aclose()


def build_model_gateway(settings: Settings) -> ModelGateway:
    if settings.model_provider is ModelProvider.MOCK:
        return MockModelGateway(settings.model_name_synth)
    assert settings.llm_base_url is not None
    assert settings.llm_api_key is not None
    return OpenAICompatibleModelGateway(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key.get_secret_value(),
        model_name=settings.model_name_synth,
        timeout_seconds=settings.model_timeout_seconds,
    )
