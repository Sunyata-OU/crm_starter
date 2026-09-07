"""Signed-cookie sessions.

The cookie holds the identity itself rather than a lookup key, so there is no
server-side session store to run or scale. That is the right trade for a
starter: it works with multiple workers out of the box. The cost is that a
session cannot be revoked before it expires, which is what ``max_age`` is for.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal, cast

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import Response

from app.core.results import Identity

log = logging.getLogger("crm.auth")

SESSION_SALT = "crm.session"

#: Key the caller's own access token is stored under, when the deployment has
#: asked for that. Short, because every byte competes with the claims for the
#: cookie's 4 KB.
TOKEN_KEY = "at"

#: Browsers drop a cookie larger than this, and drop it *silently* -- the
#: response looks fine and the next request arrives signed out. Better to
#: refuse to store the token and say so than to log everybody out.
COOKIE_LIMIT = 4096


class SessionStore:
    """Reads and writes the signed session cookie."""

    def __init__(
        self,
        secret_key: str,
        *,
        cookie_name: str = "crm_session",
        max_age: int = 60 * 60 * 12,
        secure: bool = False,
        samesite: str = "lax",
        path: str = "/",
    ) -> None:
        self.serializer = URLSafeTimedSerializer(secret_key, salt=SESSION_SALT)
        self.cookie_name = cookie_name
        self.max_age = max_age
        self.secure = secure
        self.samesite = cast(Literal["lax", "strict", "none"], samesite.lower())
        self.path = path

    # -- raw payload --------------------------------------------------------

    def read(self, request: Request) -> dict[str, Any]:
        """The session payload, or an empty dict if absent, stale or tampered."""
        raw = request.cookies.get(self.cookie_name)
        if not raw:
            return {}
        try:
            data = self.serializer.loads(raw, max_age=self.max_age)
        except SignatureExpired:
            return {}
        except BadSignature:
            # A forged or key-rotated cookie. Treated as signed-out rather than
            # as an error, so rotating the secret logs people out cleanly.
            return {}
        return data if isinstance(data, dict) else {}

    def write(self, response: Response, data: dict[str, Any]) -> None:
        payload = self.serializer.dumps(data)
        if len(payload) > COOKIE_LIMIT and TOKEN_KEY in data:
            # Dropping the token costs an API call its attribution; dropping
            # the cookie costs the session. Prefer the smaller loss, and say
            # which one was taken.
            log.warning(
                "session cookie is %d bytes with the access token in it; storing "
                "the session without it. Writes will fall back to the "
                "connection's own credentials.",
                len(payload),
            )
            data = {k: v for k, v in data.items() if k != TOKEN_KEY}
            payload = self.serializer.dumps(data)
        response.set_cookie(
            self.cookie_name,
            payload,
            max_age=self.max_age,
            httponly=True,
            secure=self.secure,
            samesite=self.samesite,
            path=self.path,
        )

    def access_token(self, request: Request) -> str:
        """The caller's own OIDC access token, if one was kept.

        Empty unless `CRM_OIDC_KEEP_ACCESS_TOKEN` is on -- see
        `app/web/routes/auth.py` for what that setting costs.
        """
        return str(self.read(request).get(TOKEN_KEY) or "")

    def clear(self, response: Response) -> None:
        response.delete_cookie(self.cookie_name, path=self.path)

    # -- identities ---------------------------------------------------------

    def load_identity(self, request: Request) -> Identity | None:
        data = self.read(request)
        subject = data.get("sub")
        if not subject:
            return None
        return Identity(
            subject=str(subject),
            email=data.get("email", ""),
            display_name=data.get("name", ""),
            roles=frozenset(data.get("roles", ())),
            claims=data.get("claims", {}),
            provider=data.get("provider", ""),
            timezone=data.get("tz", "UTC"),
            locale=data.get("locale", "en"),
            must_change_password=bool(data.get("pwchange", False)),
        )

    def save_identity(self, response: Response, identity: Identity, **extra: Any) -> None:
        self.write(
            response,
            {
                "sub": identity.subject,
                "email": identity.email,
                "name": identity.display_name,
                "roles": sorted(identity.roles),
                "provider": identity.provider,
                "tz": identity.timezone,
                "locale": identity.locale,
                # Travels with the session, so the requirement outlives the
                # request that signed the person in.
                "pwchange": identity.must_change_password,
                # Only small, non-sensitive claims belong in a cookie; a full
                # token payload would blow the 4KB limit.
                "claims": {k: v for k, v in identity.claims.items() if _is_small(v)},
                **extra,
            },
        )


def _is_small(value: Any) -> bool:
    try:
        return len(json.dumps(value)) <= 256
    except (TypeError, ValueError):
        return False
