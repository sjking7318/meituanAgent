from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID, uuid4

from redis.asyncio import Redis

from sales_assistant.domain import (
    ConcurrentWriteError,
    ConversationBusyError,
    ConversationLease,
    LeaseManager,
)
from sales_assistant.settings import AppEnvironment, Settings

_RENEW_SCRIPT = """\
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_SCRIPT = """\
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


@dataclass(slots=True)
class _RedisConversationLease:
    manager: RedisLeaseManager
    key: str
    owner: str
    _fencing_token: int
    lost: asyncio.Event

    @property
    def fencing_token(self) -> int:
        return self._fencing_token

    async def ensure_valid(self) -> None:
        if self.lost.is_set() or not await self.manager._renew(self.key, self.owner):
            self.lost.set()
            raise ConcurrentWriteError("conversation lease was lost")


class RedisLeaseManager:
    def __init__(self, redis: Redis, *, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl_ms = ttl_seconds * 1000
        self._renew_interval = max(ttl_seconds / 3, 1)

    @staticmethod
    def _lease_key(tenant_id: UUID, conversation_id: UUID) -> str:
        return f"sa:lease:{tenant_id}:{conversation_id}"

    @staticmethod
    def _fence_key(tenant_id: UUID, conversation_id: UUID) -> str:
        return f"sa:fence:{tenant_id}:{conversation_id}"

    @asynccontextmanager
    async def hold(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
    ) -> AsyncGenerator[ConversationLease]:
        key = self._lease_key(tenant_id, conversation_id)
        owner = uuid4().hex
        acquired = await self._redis.set(key, owner, nx=True, px=self._ttl_ms)
        if not acquired:
            raise ConversationBusyError("another request is already processing this conversation")

        fencing_token = int(
            await cast(
                Awaitable[int], self._redis.incr(self._fence_key(tenant_id, conversation_id))
            )
        )
        lost = asyncio.Event()
        lease = _RedisConversationLease(self, key, owner, fencing_token, lost)
        renew_task = asyncio.create_task(
            self._renew_loop(key, owner, lost),
            name=f"renew-conversation-lease-{conversation_id}",
        )
        try:
            yield lease
        finally:
            renew_task.cancel()
            with suppress(asyncio.CancelledError):
                await renew_task
            await self._release(key, owner)

    async def _renew_loop(self, key: str, owner: str, lost: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(self._renew_interval)
            if not await self._renew(key, owner):
                lost.set()
                return

    async def _renew(self, key: str, owner: str) -> bool:
        result = await cast(
            Awaitable[Any],
            self._redis.eval(_RENEW_SCRIPT, 1, key, owner, str(self._ttl_ms)),
        )
        return bool(result)

    async def _release(self, key: str, owner: str) -> None:
        await cast(Awaitable[Any], self._redis.eval(_RELEASE_SCRIPT, 1, key, owner))

    async def health_check(self) -> None:
        await self._redis.ping()

    async def close(self) -> None:
        await self._redis.aclose()


@dataclass(slots=True)
class _InMemoryConversationLease:
    lock: asyncio.Lock
    _fencing_token: int

    @property
    def fencing_token(self) -> int:
        return self._fencing_token

    async def ensure_valid(self) -> None:
        if not self.lock.locked():
            raise ConcurrentWriteError("conversation lease was lost")


class InMemoryLeaseManager:
    def __init__(self) -> None:
        self._locks: dict[tuple[UUID, UUID], asyncio.Lock] = {}
        self._fences: dict[tuple[UUID, UUID], int] = {}

    @asynccontextmanager
    async def hold(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
    ) -> AsyncGenerator[ConversationLease]:
        key = (tenant_id, conversation_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            raise ConversationBusyError("another request is already processing this conversation")
        await lock.acquire()
        self._fences[key] = self._fences.get(key, 0) + 1
        try:
            yield _InMemoryConversationLease(lock, self._fences[key])
        finally:
            if lock.locked():
                lock.release()

    async def health_check(self) -> None:
        return None

    async def close(self) -> None:
        self._locks.clear()
        self._fences.clear()


def build_lease_manager(settings: Settings) -> LeaseManager:
    if settings.app_env is AppEnvironment.TEST:
        return InMemoryLeaseManager()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    return RedisLeaseManager(redis, ttl_seconds=settings.conversation_lease_seconds)
