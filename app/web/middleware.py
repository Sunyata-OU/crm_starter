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
