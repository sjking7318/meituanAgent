from __future__ import annotations

import asyncio
from uuid import UUID

import structlog

from sales_assistant.domain import utc_now
from sales_assistant.infrastructure.mysql.database import Database
from sales_assistant.infrastructure.mysql.models import TenantRecord, UserRecord
from sales_assistant.settings import get_settings

logger = structlog.get_logger()

# Fixed demo identifiers so local requests can set X-Tenant-ID / X-User-ID.
DEMO_TENANT_ID = UUID("00000000-0000-0000-0000-0000000000a1")
DEMO_USER_ID = UUID("00000000-0000-0000-0000-0000000000b1")


async def seed() -> None:
    settings = get_settings()
    database = Database(settings.database_url)
    async with database.session_factory() as session:
        existing = await session.get(TenantRecord, DEMO_TENANT_ID)
        if existing is None:
            session.add(
                TenantRecord(
                    id=DEMO_TENANT_ID,
                    name="Demo Tenant",
                    status="active",
                    retention_days=365,
                    created_at=utc_now(),
                )
            )
            session.add(
                UserRecord(
                    id=DEMO_USER_ID,
                    tenant_id=DEMO_TENANT_ID,
                    external_subject="demo-user",
                    status="active",
                    ltm_enabled=1,
                    created_at=utc_now(),
                )
            )
            await session.commit()
            logger.info("seed_completed", tenant_id=str(DEMO_TENANT_ID), user_id=str(DEMO_USER_ID))
        else:
            logger.info("seed_skipped", reason="demo tenant already exists")
    await database.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
