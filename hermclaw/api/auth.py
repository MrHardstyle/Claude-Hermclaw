"""API authentication (P30 30.9): bearer tokens with scopes.

- Admin token: secret reference ``settings.api_token_ref`` (scope ``admin``).
- Additional tokens: ``api_tokens`` table (sha256 hashes, scopes ``read``/``control``/``admin``, revocable).
- EventSource cannot send headers, therefore SSE endpoints additionally accept ``?access_token=``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, Request
from sqlalchemy import select, update

from hermclaw.core.errors import AuthError, ConfigError, PolicyViolation
from hermclaw.core.settings import get_settings
from hermclaw.persistence.db import get_sessionmaker
from hermclaw.persistence.models import ApiToken
from hermclaw.security.secrets import SecretStore
from hermclaw.security.tokens import hash_token, tokens_equal

SCOPE_ORDER = {"read": 0, "control": 1, "admin": 2}


@dataclass(frozen=True)
class Principal:
    name: str
    scopes: frozenset[str]

    def has(self, scope: str) -> bool:
        need = SCOPE_ORDER[scope]
        return any(SCOPE_ORDER.get(s, -1) >= need for s in self.scopes)


_admin_token_cache: dict[str, str | None] = {}


def _admin_token() -> str | None:
    ref = get_settings().api_token_ref
    if ref not in _admin_token_cache:
        try:
            _admin_token_cache[ref] = SecretStore().resolve(ref, required=False)
        except ConfigError:
            _admin_token_cache[ref] = None
    return _admin_token_cache[ref]


def reset_auth_cache() -> None:
    _admin_token_cache.clear()


def _extract_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    if request.url.path.endswith("/stream"):
        return request.query_params.get("access_token")
    return None


async def authenticate(request: Request) -> Principal:
    token = _extract_token(request)
    if not token:
        raise AuthError("missing bearer token")
    admin = _admin_token()
    if admin and tokens_equal(token, admin):
        return Principal("admin", frozenset({"admin"}))
    digest = hash_token(token)
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(ApiToken).where(ApiToken.token_hash == digest, ApiToken.revoked_at.is_(None)))
        ).scalar_one_or_none()
        if row is None:
            raise AuthError("invalid token")
        await session.execute(update(ApiToken).where(ApiToken.id == row.id).values(last_used_at=datetime.now(UTC)))
        await session.commit()
        return Principal(row.name, frozenset(str(s) for s in row.scopes))


def require(scope: str):  # type: ignore[no-untyped-def]
    async def _dep(principal: Principal = Depends(authenticate)) -> Principal:
        if not principal.has(scope):
            raise PolicyViolation(f"scope '{scope}' required", code="FORBIDDEN")
        return principal

    return _dep
