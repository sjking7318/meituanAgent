"""Connectivity smoke test for the configured model gateways.

Runs three live calls against the endpoints configured in .env:
  1. LLM chat completion (DashScope)
  2. Embedding (Gitee AI)
  3. Rerank (Gitee AI)

Usage:
    uv run python -m sales_assistant.scripts.check_gateways
"""

from __future__ import annotations

import asyncio

from sales_assistant.domain import ModelRequest
from sales_assistant.domain.entities import new_id
from sales_assistant.infrastructure.model_gateway.embeddings import (
    build_embedder,
    build_reranker,
)
from sales_assistant.infrastructure.model_gateway.gateway import build_model_gateway
from sales_assistant.settings import get_settings

_OK = "\u2705"
_FAIL = "\u274c"


async def _check_llm() -> bool:
    settings = get_settings()
    gateway = build_model_gateway(settings)
    print(f"\n[LLM] provider={settings.model_provider} model={settings.model_name_synth}")
    print(f"      base_url={settings.llm_base_url}")
    try:
        response = await gateway.generate(
            ModelRequest(
                system_prompt="你是一个测试助手，请用一句话回答。",
                user_prompt="请回复：连通正常",
                conversation_id=new_id(),
                run_id=new_id(),
            )
        )
        print(f"  {_OK} 回复: {response.content[:120]}")
        print(
            f"     model={response.model} in={response.input_tokens} out={response.output_tokens}"
        )
        return True
    except Exception as error:
        print(f"  {_FAIL} LLM 调用失败: {type(error).__name__}: {error}")
        return False
    finally:
        close = getattr(gateway, "close", None)
        if close is not None:
            await close()


async def _check_embedding() -> bool:
    settings = get_settings()
    embedder = build_embedder(settings)
    print(f"\n[Embedding] model={settings.embedding_model} base_url={settings.embedding_base_url}")
    try:
        vectors = await embedder.embed(["销售话术推荐", "商家画像分析"])
        dims = [len(v) for v in vectors]
        print(f"  {_OK} 返回 {len(vectors)} 个向量，维度={dims}")
        return True
    except Exception as error:
        print(f"  {_FAIL} Embedding 调用失败: {type(error).__name__}: {error}")
        return False
    finally:
        close = getattr(embedder, "close", None)
        if close is not None:
            await close()


async def _check_rerank() -> bool:
    settings = get_settings()
    reranker = build_reranker(settings)
    print(f"\n[Rerank] model={settings.rerank_model} base_url={settings.embedding_base_url}")
    try:
        ranked = await reranker.rerank(
            "如何向新商家推荐团购套餐",
            [
                "团购套餐面向新商家的推荐话术与步骤",
                "今天的天气非常适合出门",
                "新商家入驻流程与资质要求",
            ],
            top_k=3,
        )
        print(f"  {_OK} 重排结果 (index, score): {ranked}")
        return True
    except Exception as error:
        print(f"  {_FAIL} Rerank 调用失败: {type(error).__name__}: {error}")
        return False
    finally:
        close = getattr(reranker, "close", None)
        if close is not None:
            await close()


async def main() -> int:
    print("=== 模型网关连通性测试 ===")
    results = {
        "LLM": await _check_llm(),
        "Embedding": await _check_embedding(),
        "Rerank": await _check_rerank(),
    }
    print("\n=== 汇总 ===")
    for name, ok in results.items():
        print(f"  {_OK if ok else _FAIL} {name}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
