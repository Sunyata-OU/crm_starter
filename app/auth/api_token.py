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
import logging
import secrets
from datetime import datetime, timedelta

from starlette.requests import Request

from app.auth.base import AuthError, BaseAuthProvider
from app.core.clock import parse as parse_instant
from app.core.clock import utcnow
from app.core.query import Condition, ListQuery, Op
from app.core.results import Ctx, Identity, Record
from app.providers.base import Provider

log = logging.getLogger("crm.auth")

TOKEN_PREFIX = "crm_"

#: How stale ``last_used`` is allowed to get before it is written again.
#:
#: Recording every single call would put a write on the hot path of an API
#: whose whole point is being cheap to call, and the question the column
#: answers -- "is this token still in use, and from where?" -- is not one that
#: needs minute-by-minute resolution. A changed address is written immediately
#: regardless, because that is the observation somebody might act on.
TOUCH_AFTER = timedelta(minutes=15)


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
        expires_field: str = "expires_at",
        header: str = "Authorization",
        scheme: str = "Bearer",
        #: Believe ``X-Forwarded-For`` when recording where a token was used.
        #: Off unless the deployment has said it trusts its proxy, on the same
        #: reasoning as the audit log's address: an untrusted header lets a
        #: caller write its own origin into the record of its own calls.
        trust_forwarded_for: bool = False,
    ) -> None:
        self.tokens = tokens
        self.hash_field = hash_field
        self.subject_field = subject_field
        self.roles_field = roles_field
        self.active_field = active_field
        self.expires_field = expires_field
        self.header = header
        self.scheme = scheme
        self.trust_forwarded_for = trust_forwarded_for

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
        if record is None or not self._is_active(record) or self._has_expired(record):
            # A token *was* presented and is not valid. Raising stops the chain
            # so a bad token cannot fall through to an anonymous read.
            #
            # One message for all three cases, deliberately. Telling a caller
            # that their token exists but has expired is telling somebody
            # holding a guessed value that they guessed right.
            raise AuthError("That API token is not valid.", provider=self.name)

        await self._record_use(record, self._caller_ip(request))

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

    def _has_expired(self, record: Record, now: datetime | None = None) -> bool:
        """Whether this token's date has passed. No date means it has not.

        Unparseable is treated as expired. A column holding something this
        cannot read is a column nobody can reason about, and the safe reading
        of an unreadable expiry date is that the credential is not usable.
        """
        raw = record.get(self.expires_field)
        if not raw:
            return False
        expires = parse_instant(raw)
        if expires is None:
            log.warning("api token %s has an unreadable expiry %r", record.pk, raw)
            return True
        return expires <= (now or utcnow())

    def _caller_ip(self, request: Request) -> str:
        if self.trust_forwarded_for:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",")[0].strip()[:64]
        client = request.client
        return client.host[:64] if client else ""

    async def _record_use(self, record: Record, ip: str) -> None:
        """Note that this token was used, and from where.

        Never allowed to fail the request it is describing: a token that works
        must not stop working because the bookkeeping write did not land. The
        write is skipped entirely while the record is fresh -- see
        ``TOUCH_AFTER`` -- so the common case costs nothing.
        """
        now = utcnow()
        last = parse_instant(record.get("last_used"))
        moved = ip and str(record.get("last_used_ip") or "") != ip
        if last is not None and not moved and now - last < TOUCH_AFTER:
            return
        try:
            await self.tokens.update(
                record.pk, {"last_used": now, "last_used_ip": ip or None}, Ctx.system()
            )
        except Exception:
            log.warning("could not record the use of api token %s", record.pk, exc_info=True)
