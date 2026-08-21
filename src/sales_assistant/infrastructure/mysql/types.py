from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import BINARY
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UuidBinary(TypeDecorator[uuid.UUID]):
    """Store UUID as BINARY(16) in MySQL (ADR-0001).

    Application generates UUIDv7 for time-ordered, index-friendly keys.
    """

    impl = BINARY(16)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> bytes | None:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value.bytes
        return uuid.UUID(str(value)).bytes

    def process_result_value(self, value: Any, dialect: Dialect) -> uuid.UUID | None:
        if value is None:
            return None
        return uuid.UUID(bytes=bytes(value))
