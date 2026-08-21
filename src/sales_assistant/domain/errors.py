from __future__ import annotations


class DomainError(Exception):
    code = "DOMAIN_ERROR"


class AuthenticationError(DomainError):
    code = "AUTHENTICATION_FAILED"


class ResourceNotFoundError(DomainError):
    code = "RESOURCE_NOT_FOUND"


class ResourceForbiddenError(DomainError):
    code = "RESOURCE_FORBIDDEN"


class ConcurrentWriteError(DomainError):
    code = "CONCURRENT_WRITE"


class ConversationBusyError(DomainError):
    code = "CONVERSATION_BUSY"


class DependencyUnavailableError(DomainError):
    code = "DEPENDENCY_UNAVAILABLE"


class IdempotencyConflictError(DomainError):
    code = "IDEMPOTENCY_CONFLICT"


class FeatureNotImplementedError(DomainError):
    code = "FEATURE_NOT_IMPLEMENTED"


class InvalidStateTransitionError(DomainError):
    code = "INVALID_STATE_TRANSITION"


class SkillError(DomainError):
    """A Skill/Tool failed validation, timed out, or was not found."""

    code = "SKILL_ERROR"
