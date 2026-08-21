from __future__ import annotations

from typing import Annotated, Any, TypedDict
from uuid import UUID


def _replace(_current: Any, new: Any) -> Any:
    """Channel reducer: last write wins (kept explicit for clarity)."""
    return new


class RunState(TypedDict, total=False):
    """Typed LangGraph state (agent-design.md 2).

    Only structured fields are persisted to checkpoints. Prompt text, large
    tool payloads and raw documents are never placed here.
    """

    run_id: str
    tenant_id: str
    user_id: str
    conversation_id: str
    user_query: str
    standalone_query: str | None
    route: Annotated[dict[str, Any] | None, _replace]
    evidence: Annotated[list[dict[str, Any]], _replace]
    citations: Annotated[list[dict[str, Any]], _replace]
    answer: Annotated[str | None, _replace]
    model: Annotated[str | None, _replace]
    input_tokens: Annotated[int, _replace]
    output_tokens: Annotated[int, _replace]


def initial_state(
    *,
    run_id: UUID,
    tenant_id: UUID,
    user_id: UUID,
    conversation_id: UUID,
    user_query: str,
) -> RunState:
    return RunState(
        run_id=str(run_id),
        tenant_id=str(tenant_id),
        user_id=str(user_id),
        conversation_id=str(conversation_id),
        user_query=user_query,
        standalone_query=None,
        route=None,
        evidence=[],
        answer=None,
        model=None,
        input_tokens=0,
        output_tokens=0,
    )
