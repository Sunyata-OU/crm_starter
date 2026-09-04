"""Messages that survive a redirect.

The application says things to people at moments when it is also navigating
them somewhere: "Saved", "That link has expired", "Temporary password: …". An
HTMX request can carry the message back in a header and let the page show it
without navigating. A plain form post cannot -- it answers with a redirect, and
a header on a redirect is discarded by the browser.

Without somewhere to put the message, those paths silently say nothing. That is
the failure this exists to prevent: not an error page, but an action that
appears to have done nothing at all.

So a message that cannot be delivered in a header is parked in a short-lived
signed cookie and rendered by the next page. Signed because the cookie is
written by us and read back as page content; nothing here is secret, but a
message a third party can put on the screen of someone who trusts this site is
a phishing tool.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Literal

from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import Response

log = logging.getLogger("crm.flash")

COOKIE = "crm_flash"
SALT = "crm.flash"
#: Long enough to survive a redirect, short enough that a message never
#: reappears in a session started an hour later.
MAX_AGE = 120
#: Cookies are limited, and a queue this long means something is looping.
MAX_MESSAGES = 5

Level = Literal["success", "info", "warning", "error"]
SameSite = Literal["lax", "strict", "none"]


@dataclass(frozen=True, slots=True)
class Message:
    text: str
    level: Level = "success"

    @property
    def is_error(self) -> bool:
        return self.level == "error"


class FlashStore:
    """Reads and writes the pending-message cookie."""

    def __init__(
        self,
        secret_key: str,
        *,
        secure: bool = False,
        samesite: SameSite = "lax",
    ) -> None:
        self.serializer = URLSafeTimedSerializer(secret_key, salt=SALT)
        self.secure = secure
        self.samesite = samesite

    def read(self, request: Request) -> list[Message]:
        raw = request.cookies.get(COOKIE)
        if not raw:
            return []
        try:
            payload = self.serializer.loads(raw, max_age=MAX_AGE)
        except (BadSignature, ValueError):
            # A stale or forged cookie is simply no messages. Nothing here is
            # important enough to fail a page over.
            return []
        if not isinstance(payload, list):
            return []
        return [
            Message(str(item.get("t", "")), item.get("l", "info"))
            for item in payload
            if isinstance(item, dict) and item.get("t")
        ][:MAX_MESSAGES]

    def add(self, request: Request, response: Response, text: str, level: Level) -> None:
        """Queue a message for the next page this browser renders."""
        pending = [*self.read(request), Message(text, level)][-MAX_MESSAGES:]
        value = self.serializer.dumps(
            [{"t": m.text, "l": m.level} for m in pending]
        )
        if len(value) > 3500:  # leave room under the 4KB cookie limit
            log.warning("flash message too large to carry; dropped")
            return
        response.set_cookie(
            COOKIE, value, max_age=MAX_AGE, httponly=True,
            secure=self.secure, samesite=self.samesite, path="/",
        )

    def clear(self, response: Response) -> None:
        response.delete_cookie(COOKIE, path="/")


def as_trigger(text: str, level: str) -> str:
    """The HX-Trigger payload the client-side toast listens for."""
    return json.dumps({"crm:toast": {"message": text, "level": level}})
