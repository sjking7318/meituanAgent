from __future__ import annotations

from uuid import UUID

from sales_assistant.domain import AuthContext, AuthenticationError
from sales_assistant.settings import AuthMode, Settings


class Authenticator:
    """Authentication skeleton.

    - disabled mode: trust X-Tenant-ID / X-User-ID headers (local/dev only).
    - oidc mode: JWT validation is a follow-up task (T-102); currently rejects
      to avoid a false sense of security in non-local environments.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def authenticate(
        self,
        *,
        authorization: str | None,
        tenant_header: str | None,
        user_header: str | None,
    ) -> AuthContext:
        if self._settings.auth_mode is AuthMode.DISABLED:
            return self._from_headers(tenant_header, user_header)
        raise AuthenticationError("OIDC authentication is not yet implemented")

    @staticmethod
    def _from_headers(tenant_header: str | None, user_header: str | None) -> AuthContext:
        if not tenant_header or not user_header:
            raise AuthenticationError("X-Tenant-ID and X-User-ID headers are required")
        try:
            tenant_id = UUID(tenant_header)
            user_id = UUID(user_header)
        except ValueError as error:
            raise AuthenticationError("tenant/user identifiers must be UUIDs") from error
        return AuthContext(tenant_id=tenant_id, user_id=user_id)
