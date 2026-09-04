"""Turning exceptions into responses.

The same failure has to be presentable three ways -- a full error page, an HTMX
fragment that lands inside the page, and a JSON body -- so the handling lives
here rather than being repeated per route.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, RedirectResponse, Response

from app.auth.base import AuthError
from app.core.errors import AuthenticationRequired, CRMError, MustChangePassword
from app.web import htmx

log = logging.getLogger("crm.errors")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AuthenticationRequired)
    async def _unauthenticated(request: Request, exc: AuthenticationRequired) -> Response:
        """Send a browser to sign in; tell an API client plainly."""
        if htmx.wants_json(request):
            return JSONResponse({"error": exc.public_message}, status_code=401)
        next_url = request.url.path
        if request.url.query:
            next_url += f"?{request.url.query}"
        login = f"/login?next={next_url}"
        if htmx.read_htmx(request).is_htmx:
            # A fragment request cannot follow a redirect usefully; tell HTMX to
            # navigate the whole page instead.
            return htmx.redirect(Response(status_code=204), login)
        return RedirectResponse(login, status_code=303)

    @app.exception_handler(MustChangePassword)
    async def _must_change_password(request: Request, exc: MustChangePassword) -> Response:
        """Send them to the one page they are allowed to use.

        A redirect rather than an error page, because the person has nothing to
        fix except by going there, and an error page with a link is a worse
        version of just going.
        """
        if htmx.wants_json(request):
            return JSONResponse({"error": exc.message}, status_code=403)
        target = "/account/password?required=1"
        if htmx.read_htmx(request).is_htmx:
            return htmx.redirect(Response(status_code=204), target)
        return RedirectResponse(target, status_code=303)

    @app.exception_handler(AuthError)
    async def _rejected_credentials(request: Request, exc: AuthError) -> Response:
        """Credentials were presented and are wrong.

        Distinct from AuthenticationRequired, which means none were presented:
        sending a client that already tried a bad token to a login page would
        be useless, and returning 500 would suggest the fault is ours.
        """
        log.info("authentication refused by %s: %s", exc.provider, exc.message)
        if htmx.wants_json(request):
            return JSONResponse({"error": exc.message}, status_code=401)
        return _render_error(request, 401, exc.message, "AuthError")

    @app.exception_handler(CRMError)
    async def _crm_error(request: Request, exc: CRMError) -> Response:
        if exc.status_code >= 500:
            log.exception("%s: %s", type(exc).__name__, exc.message, exc_info=exc)
        else:
            log.info("%s: %s", type(exc).__name__, exc.message)
        return _render_error(request, exc.status_code, exc.message, type(exc).__name__)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> Response:
        return _render_error(request, exc.status_code, str(exc.detail), "HTTPException")

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> Response:
        log.exception("unhandled error at %s", request.url.path, exc_info=exc)
        state = getattr(request.app.state, "crm", None)
        debug = bool(state and state.settings.debug)
        # The real message only in debug; otherwise it may carry connection
        # strings or row contents.
        message = f"{type(exc).__name__}: {exc}" if debug else "Something went wrong."
        return _render_error(request, 500, message, type(exc).__name__)


def _render_error(request: Request, status: int, message: str, kind: str) -> Response:
    if htmx.wants_json(request):
        return JSONResponse({"error": message, "type": kind}, status_code=status)

    state = getattr(request.app.state, "crm", None)
    if state is None:
        return Response(message, status_code=status, media_type="text/plain")

    info = htmx.read_htmx(request)
    template = "errors/_fragment.html" if info.wants_fragment else "errors/error.html"
    try:
        html = state.templates.render(
            template,
            {
                "request": request,
                "status": status,
                "message": message,
                "kind": kind,
                "title": TITLES.get(status, "Error"),
                "settings": state.settings,
                "identity": None,
                "menu": [],
            },
        )
    except Exception:
        log.exception("the error template itself failed")
        return Response(message, status_code=status, media_type="text/plain")

    response = Response(html, status_code=status, media_type="text/html")
    if info.wants_fragment:
        # Without this HTMX ignores an error response, leaving the user with a
        # button that appears to do nothing.
        response.headers["HX-Reswap"] = "innerHTML"
    return response


TITLES = {
    400: "Bad request",
    401: "Sign in required",
    403: "Not allowed",
    404: "Not found",
    409: "Conflict",
    422: "Check the form",
    501: "Not supported",
    502: "Backend unavailable",
    507: "Too much data",
}
