from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from redis.asyncio import Redis

from sales_assistant.domain import RunEventStream, StoredRunEvent, utc_now
from sales_assistant.settings import AppEnvironment, Settings


def _event_id_key(event_id: str) -> tuple[int, int]:
    milliseconds, sequence = event_id.split("-", maxsplit=1)
    return int(milliseconds), int(sequence)


class RedisRunEventStream:
    def __init__(self, redis: Redis, *, retention_seconds: int) -> None:
        self._redis = redis
        self._retention_seconds = retention_seconds

    @staticmethod
    def _key(run_id: UUID) -> str:
        return f"sa:run-events:{run_id}"

    async def append(
        self,
        run_id: UUID,
        event_type: str,
        data: dict[str, Any],
    ) -> StoredRunEvent:
        created_at = utc_now()
        key = self._key(run_id)
        event_id = await cast(
            Awaitable[str],
            self._redis.xadd(
                key,
                {
                    "event_type": event_type,
                    "data": json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                    "created_at": created_at.isoformat(),
                },
                maxlen=2_000,
                approximate=True,
            ),
        )
        await self._redis.expire(key, self._retention_seconds)
        return StoredRunEvent(
            id=event_id,
            run_id=run_id,
            event_type=event_type,
            data=data,
            created_at=created_at,
        )

    async def read(
        self,
        run_id: UUID,
        *,
        after_id: str,
        block_milliseconds: int,
        limit: int = 100,
    ) -> list[StoredRunEvent]:
        raw = await cast(
            Awaitable[Any],
            self._redis.xread(
                {self._key(run_id): after_id},
                count=limit,
                block=block_milliseconds or None,
            ),
        )
        events: list[StoredRunEvent] = []
        for _, entries in raw:
            for event_id, fields in entries:
                events.append(
                    StoredRunEvent(
                        id=str(event_id),
                        run_id=run_id,
                        event_type=str(fields["event_type"]),
                        data=cast(dict[str, Any], json.loads(fields["data"])),
                        created_at=datetime.fromisoformat(fields["created_at"]),
                    )
                )
        return events

    async def health_check(self) -> None:
        await self._redis.ping()

    async def close(self) -> None:
        await self._redis.aclose()


class InMemoryRunEventStream:
    def __init__(self) -> None:
        self._events: dict[UUID, list[StoredRunEvent]] = {}
        self._conditions: dict[UUID, asyncio.Condition] = {}

    async def append(
        self,
        run_id: UUID,
        event_type: str,
        data: dict[str, Any],
    ) -> StoredRunEvent:
        condition = self._conditions.setdefault(run_id, asyncio.Condition())
        async with condition:
            events = self._events.setdefault(run_id, [])
            event = StoredRunEvent(
                id=f"{len(events) + 1}-0",
                run_id=run_id,
                event_type=event_type,
                data=data,
                created_at=datetime.now(UTC),
            )
            events.append(event)
            condition.notify_all()
            return event

    async def read(
        self,
        run_id: UUID,
        *,
        after_id: str,
        block_milliseconds: int,
        limit: int = 100,
    ) -> list[StoredRunEvent]:
        condition = self._conditions.setdefault(run_id, asyncio.Condition())

        def available() -> list[StoredRunEvent]:
            after = _event_id_key(after_id)
            return [
                event for event in self._events.get(run_id, []) if _event_id_key(event.id) > after
            ][:limit]

        async with condition:
            events = available()
            if events or block_milliseconds <= 0:
                return events
            try:
                await asyncio.wait_for(condition.wait(), timeout=block_milliseconds / 1_000)
            except TimeoutError:
                return []
            return available()

    async def health_check(self) -> None:
        return None

    async def close(self) -> None:
        self._events.clear()
        self._conditions.clear()


def build_run_event_stream(settings: Settings) -> RunEventStream:
    if settings.app_env is AppEnvironment.TEST:
        return InMemoryRunEventStream()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    return RedisRunEventStream(redis, retention_seconds=settings.sse_retention_seconds)
