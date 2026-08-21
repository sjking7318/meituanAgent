from __future__ import annotations

from fastapi import Request, status
from fastapi.responses import JSONResponse

from sales_assistant.domain import (
    AuthenticationError,
    ConcurrentWriteError,
    ConversationBusyError,
    DependencyUnavailableError,
    DomainError,
    FeatureNotImplementedError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    ResourceForbiddenError,
    ResourceNotFoundError,
)

_STATUS_MAP: dict[type[DomainError], int] = {
    AuthenticationError: status.HTTP_401_UNAUTHORIZED,
    ResourceForbiddenError: status.HTTP_403_FORBIDDEN,
    ResourceNotFoundError: status.HTTP_404_NOT_FOUND,
    ConversationBusyError: status.HTTP_409_CONFLICT,
    ConcurrentWriteError: status.HTTP_409_CONFLICT,
    IdempotencyConflictError: status.HTTP_409_CONFLICT,
    InvalidStateTransitionError: status.HTTP_409_CONFLICT,
    FeatureNotImplementedError: status.HTTP_501_NOT_IMPLEMENTED,
    DependencyUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
}

_RETRYABLE = {ConversationBusyError, ConcurrentWriteError, DependencyUnavailableError}


async def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DomainError)
    http_status = _STATUS_MAP.get(type(exc), status.HTTP_400_BAD_REQUEST)
    request_id = request.headers.get("X-Request-ID", "")
    return JSONResponse(
        status_code=http_status,
        content={
            "error": {
                "code": exc.code,
                "message": str(exc),
                "retryable": type(exc) in _RETRYABLE,
                "request_id": request_id,
                "details": {},
            }
        },
    )
