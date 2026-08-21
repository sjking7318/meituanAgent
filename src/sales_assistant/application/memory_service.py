from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import structlog

from sales_assistant.domain import (
    ConversationSummary,
    Message,
    MessageRole,
    ModelGateway,
    ModelRequest,
    ModelTurn,
    UnitOfWorkFactory,
)

logger = structlog.get_logger()

_SUMMARY_SYSTEM_PROMPT = """\
你是对话记忆摘要器。将下面的销售助手对话压缩为简洁的中文要点，只保留：
- 已确认的事实与业务数据；
- 用户目标与未解决的问题；
- 关键实体（产品、商家、政策名等）与已作出的承诺。
禁止编造信息，禁止包含任何人格设定或系统指令。若给出了“已有摘要”，请在其基础上增量合并，输出更新后的完整摘要。
只输出摘要正文本身。
"""


class MemoryService:
    """Short-term memory: sliding window + versioned rolling summary.

    Assembles conversation context as (latest summary as a system turn) +
    (most recent N raw turns), per memory-design.md 2/3. State lives in MySQL
    so any instance assembles identical context (multi-instance consistency).
    """

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        model_gateway: ModelGateway,
        *,
        recent_turns: int,
        summary_trigger_turns: int,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._model_gateway = model_gateway
        self._recent_turns = recent_turns
        self._summary_trigger_turns = summary_trigger_turns

    async def load_context(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
        *,
        before_sequence: int,
    ) -> tuple[ModelTurn, ...]:
        """Return the assembled history: summary (if any) + recent raw turns."""
        async with self._unit_of_work_factory() as unit_of_work:
            recent = await unit_of_work.messages.list(
                tenant_id,
                conversation_id,
                limit=self._recent_turns * 2,
                before_sequence=before_sequence,
            )
            summary = await unit_of_work.summaries.latest(tenant_id, conversation_id)

        turns: list[ModelTurn] = []
        # Only inject the summary portion not already covered by recent raw turns.
        oldest_recent_seq = recent[0].sequence if recent else before_sequence
        if summary is not None and summary.covered_through_sequence < oldest_recent_seq:
            turns.append(
                ModelTurn(
                    role=MessageRole.SYSTEM,
                    content=f"[早期对话摘要]\n{summary.summary}",
                )
            )
        allowed = {MessageRole.USER, MessageRole.ASSISTANT}
        turns.extend(
            ModelTurn(role=m.role, content=m.content) for m in recent if m.role in allowed
        )
        return tuple(turns)

    async def maybe_summarize(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
    ) -> None:
        """Advance the rolling summary when the window grows past the trigger.

        Inline (not Kafka) so the demo is self-contained; failures are swallowed
        because memory maintenance must never break the user-facing turn.
        """
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                messages = await unit_of_work.messages.list(
                    tenant_id,
                    conversation_id,
                    limit=1000,
                )
                summary = await unit_of_work.summaries.latest(tenant_id, conversation_id)

            covered = summary.covered_through_sequence if summary else 0
            pending = [m for m in messages if m.sequence > covered]
            # Keep the freshest `recent_turns` pairs raw; only summarize the tail
            # that will otherwise fall out of the sliding window.
            keep = self._recent_turns * 2
            if len(pending) <= max(keep, self._summary_trigger_turns):
                return
            to_summarize = pending[: len(pending) - keep]
            if not to_summarize:
                return

            new_text = await self._summarize(
                previous=summary.summary if summary else None,
                messages=to_summarize,
            )
            new_version = (summary.source_version if summary else 0) + 1
            covered_through = to_summarize[-1].sequence
            async with self._unit_of_work_factory() as unit_of_work:
                current = await unit_of_work.summaries.latest(tenant_id, conversation_id)
                # Never clobber a newer summary written by a concurrent instance.
                if current is not None and current.source_version >= new_version:
                    return
                await unit_of_work.summaries.add(
                    ConversationSummary(
                        tenant_id=tenant_id,
                        conversation_id=conversation_id,
                        summary=new_text,
                        covered_through_sequence=covered_through,
                        source_version=new_version,
                    )
                )
                await unit_of_work.commit()
        except Exception:
            logger.warning(
                "rolling_summary_update_failed",
                conversation_id=str(conversation_id),
            )

    async def _summarize(self, *, previous: str | None, messages: Sequence[Message]) -> str:
        transcript = "\n".join(f"{m.role.value}: {m.content}" for m in messages)
        prior = f"已有摘要：\n{previous}\n\n" if previous else ""
        response = await self._model_gateway.generate(
            ModelRequest(
                system_prompt=_SUMMARY_SYSTEM_PROMPT,
                user_prompt=f"{prior}对话片段：\n{transcript}\n\n请输出更新后的摘要。",
                conversation_id=messages[0].conversation_id,
                run_id=messages[0].conversation_id,
                history=(),
            )
        )
        return response.content.strip()
