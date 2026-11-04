"""Cross-cutting request handling.

Three concerns that every route needs and none should implement: an identifier
that ties a request to the audit rows and log lines it produced, a measurement
of what it cost, and compression on the way out.

Kept as middleware rather than dependencies because they must apply to error
responses too -- a request that 500s is exactly the one you want the timing and
the request id for.
"""

from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from app.auth.base import AuthError
from app.auth.oidc import OIDCAuth
from app.auth.session import EXPIRES_KEY, REFRESH_KEY, TOKEN_KEY
from app.core.errors import ConfigError
from app.core.instrument import measure, report

log = logging.getLogger("crm.request")

#: Trusted from the caller so a request can be followed across services, and
#: generated when absent.
REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Give each request an id, and measure what it costs.

    The headers are only added when ``debug`` is on: the query count is useful
    on your own machine and is a small piece of internal detail to hand to
    everyone else. The slow-request log, by contrast, stays on everywhere,
    because that is what tells you a page regressed in production.
    """

    def __init__(
        self,
        app,  # noqa: ANN001  (Starlette's ASGIApp)
        *,
        debug: bool = False,
        warn_queries: int = 25,
        warn_ms: float = 1000.0,
    ) -> None:
        super().__init__(app)
        self.debug = debug
        self.warn_queries = warn_queries
        self.warn_ms = warn_ms

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:12]
        request.state.request_id = request_id

        started = time.perf_counter()
        with measure() as stats:
            try:
                response = await call_next(request)
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1000

        report(
            stats,
            label=f"{request.method} {request.url.path}",
            total_ms=elapsed_ms,
            warn_queries=self.warn_queries,
            warn_ms=self.warn_ms,
        )

        response.headers[REQUEST_ID_HEADER] = request_id
        if self.debug:
            response.headers["Server-Timing"] = (
                f"db;dur={stats.query_ms:.1f}, total;dur={elapsed_ms:.1f}"
            )
            response.headers["X-Query-Count"] = str(stats.queries)
        return response


class SessionRefreshMiddleware(BaseHTTPMiddleware):
    """Renew an OIDC session shortly before its access token runs out.

    Runs before the route, so the request that triggers a refresh already sees
    the fresh token; the new cookie is also set on the response. Only acts on a
    session that carries a refresh token, which only exists when
    ``oidc_refresh_tokens`` is on, so everyone else pays one cookie read.

    Outcomes: the issuer refuses the refresh token (session ended or revoked
    there) -> the session is dropped and the person is signed out. The issuer
    is unreachable or errors -> the request carries on with the session it has,
    and the next one tries again; an outage is not a sign-out.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        crm = getattr(request.app.state, "crm", None)
        settings = getattr(crm, "settings", None)
        if crm is None or not settings or not settings.oidc_refresh_tokens:
            return await call_next(request)

        sessions = crm.sessions
        refresh_token, expires = sessions.refresh_state(request)
        if not refresh_token or expires - time.time() > settings.oidc_refresh_leeway:
            return await call_next(request)

        provider = crm.auth.get("oidc")
        if not isinstance(provider, OIDCAuth):
            return await call_next(request)

        new_cookie: str | None = None
        signed_out = False
        try:
            tokens = await provider.refresh(refresh_token)
        except AuthError:
            signed_out = True
            log.info("oidc refresh refused; ending the session")
        except ConfigError as exc:
            log.warning("oidc refresh failed, keeping the session for now: %s", exc)
        else:
            data = sessions.read(request)
            data[REFRESH_KEY] = tokens.get("refresh_token") or refresh_token
            data[EXPIRES_KEY] = int(time.time()) + int(tokens.get("expires_in") or 300)
            if settings.oidc_keep_access_token:
                data[TOKEN_KEY] = tokens["access_token"]
            new_cookie = sessions.encode(data)

        if signed_out or new_cookie is not None:
            _replace_cookie(request, sessions.cookie_name, new_cookie)

        response = await call_next(request)
        if signed_out:
            sessions.clear(response)
        elif new_cookie is not None:
            sessions.write_encoded(response, new_cookie)
        return response


def _replace_cookie(request: Request, name: str, value: str | None) -> None:
    """Rewrite this request's own Cookie header so the route sees the change."""
    kept = []
    for key, raw in request.scope["headers"]:
        if key != b"cookie":
            kept.append((key, raw))
            continue
        parts = [p for p in raw.decode("latin-1").split(";") if p.strip().partition("=")[0] != name]
        if parts:
            kept.append((key, ";".join(parts).encode("latin-1")))
    if value is not None:
        kept.append((b"cookie", f"{name}={value}".encode("latin-1")))
    request.scope["headers"] = kept


class CachedStaticFiles(StaticFiles):
    """Static files with an explicit cache lifetime.

    Starlette already answers a conditional request with a 304, which saves the
    body but not the round trip. ``Cache-Control`` saves the round trip too.

    The default lifetime is deliberately short. The vendored assets are served
    under stable names, so a long cache would strand browsers on an old copy
    after an upgrade. A deployment that adds a content hash to the filenames --
    the usual arrangement behind a CDN -- should raise ``static_max_age`` to a
    year and set ``immutable``.
    """

    def __init__(self, *args, max_age: int = 3600, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.max_age = max_age

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = f"public, max-age={self.max_age}"
        return response
