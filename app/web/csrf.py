"""Cross-site request forgery protection.

Tokens are signed rather than stored, so there is nothing to keep in sync across
workers. The token is bound to the session so one user's token cannot be used
in another's browser.
"""

from __future__ import annotations

import hmac

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.requests import Request

from app.core.errors import PermissionDenied

FORM_FIELD = "csrf_token"
HEADER = "X-CSRF-Token"
MAX_AGE = 60 * 60 * 12

#: Methods that cannot change state, so need no token.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class CSRFProtection:
    def __init__(self, secret_key: str, *, enabled: bool = True, max_age: int = MAX_AGE) -> None:
        self.serializer = URLSafeTimedSerializer(secret_key, salt="crm.csrf")
        self.enabled = enabled
        self.max_age = max_age

    def issue(self, session_id: str = "") -> str:
        """A token for this session, embedded in every form."""
        return self.serializer.dumps({"sid": session_id})

    def check(self, token: str, session_id: str = "") -> bool:
        if not token:
            return False
        try:
            payload = self.serializer.loads(token, max_age=self.max_age)
        except (BadSignature, SignatureExpired):
            return False
        # Compared in constant time, though the token is signed rather than secret.
        return hmac.compare_digest(str(payload.get("sid", "")), session_id)

    async def validate(self, request: Request, session_id: str = "") -> None:
        """Raise unless the request carries a valid token.

        Bearer-authenticated API calls are exempt: they do not rely on ambient
        cookie credentials, so there is nothing for a cross-site form to forge.
        """
        if not self.enabled or request.method in SAFE_METHODS:
            return
        if request.headers.get("authorization", "").lower().startswith("bearer "):
            return

        token = request.headers.get(HEADER, "")
        if not token:
            form = await request.form()
            token = str(form.get(FORM_FIELD, ""))
        if not self.check(token, session_id):
            raise PermissionDenied(
                "This form has expired or came from somewhere unexpected. "
                "Reload the page and try again."
            )
