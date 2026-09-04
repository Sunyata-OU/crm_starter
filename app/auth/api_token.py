"""Bearer tokens for machine callers.

Tokens address the JSON API rather than the HTML views, which is why this
provider never redirects: an unauthenticated API call should get a 401, not a
login page.

Only hashes are stored. A token is shown once, at creation, and cannot be
recovered afterwards.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from starlette.requests import Request

from app.auth.base import AuthError, BaseAuthProvider
from app.core.query import Condition, ListQuery, Op
from app.core.results import Ctx, Identity, Record
from app.providers.base import Provider

TOKEN_PREFIX = "crm_"


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 rather than Argon2, deliberately.

    A token is already high-entropy random, so it needs no protection against
    brute force -- and it is verified on every API request, where a deliberately
    slow hash would be a denial-of-service vector.
    """
    return hashlib.sha256(token.encode()).hexdigest()


class ApiTokenAuth(BaseAuthProvider):
    """Identifies a caller from an ``Authorization: Bearer`` header."""

    name = "api_token"
    interactive = False

    def __init__(
        self,
        tokens: Provider,
        *,
        hash_field: str = "token_hash",
        subject_field: str = "name",
        roles_field: str = "roles",
        active_field: str = "is_active",
        header: str = "Authorization",
        scheme: str = "Bearer",
    ) -> None:
        self.tokens = tokens
        self.hash_field = hash_field
        self.subject_field = subject_field
        self.roles_field = roles_field
        self.active_field = active_field
        self.header = header
        self.scheme = scheme

    def extract_token(self, request: Request) -> str | None:
        raw = request.headers.get(self.header, "")
        if not raw:
            return None
        scheme, _, value = raw.partition(" ")
        if scheme.lower() != self.scheme.lower():
            return None
        value = value.strip()
        return value or None

    async def authenticate(self, request: Request) -> Identity | None:
        token = self.extract_token(request)
        if token is None:
            # No bearer header: not our request. Defer rather than reject.
            return None

        record = await self._lookup(hash_token(token))
        if record is None or not self._is_active(record):
            # A token *was* presented and is not valid. Raising stops the chain
            # so a bad token cannot fall through to an anonymous read.
            raise AuthError("That API token is not valid.", provider=self.name)

        from app.auth.local import _parse_roles

        return Identity(
            subject=str(record.get(self.subject_field) or record.pk),
            display_name=str(record.get(self.subject_field) or "API client"),
            roles=frozenset(_parse_roles(record.get(self.roles_field))),
            provider=self.name,
        )

    async def _lookup(self, token_hash: str) -> Record | None:
        page = await self.tokens.list(
            ListQuery(
                filter=Condition(self.hash_field, Op.EQ, token_hash),
                page_size=2,
                with_total=False,
            ),
            Ctx.system(),
        )
        items = list(page.items)
        if not items:
            return None
        # Constant-time confirmation, in case the provider matched loosely.
        stored = str(items[0].get(self.hash_field, ""))
        return items[0] if hmac.compare_digest(stored, token_hash) else None

    def _is_active(self, record: Record) -> bool:
        value = record.get(self.active_field, True)
        return value if isinstance(value, bool) else str(value).lower() not in ("0", "false", "no")
