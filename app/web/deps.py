"""What a route handler is given.

One object, :class:`View`, carries everything a handler needs: the request, who
is calling, the registry, the templates and the helpers for building a response.
Handlers therefore take a single dependency instead of six, and a helper that
needs more context later does not change every signature.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.auth.chain import AuthChain
from app.core.clock import is_valid_timezone
from app.core.errors import AuthenticationRequired, MustChangePassword, NotFound
from app.core.query import clamp_page_size
from app.core.registry import Registry
from app.core.results import Ctx, Identity, Record
from app.resources.resource import Resource
from app.settings import Settings
from app.web import htmx
from app.web.csrf import CSRFProtection
from app.web.htmx import HtmxInfo
from app.web.render import Templates


@dataclass(slots=True)
class AppState:
    """Everything built once at startup and shared by every request."""

    settings: Settings
    registry: Registry
    templates: Templates
    auth: AuthChain
    csrf: CSRFProtection
    sessions: Any
    flash: Any
    #: Bounds sign-in attempts per source address.
    login_limiter: Any


#: View kinds whose template includes the search-and-filter toolbar.
TOOLBAR_VIEWS = frozenset(
    {"list", "board", "calendar", "chart", "pivot", "tree", "gantt", "map", "activity"}
)


class View:
    """Per-request context and response helpers."""

    def __init__(self, request: Request, state: AppState, identity: Identity) -> None:
        self.request = request
        self.state = state
        self.identity = identity
        self.htmx: HtmxInfo = htmx.read_htmx(request)
        # Set by RequestContextMiddleware, which also puts it on the response
        # and on any slow-request log line. The fallback covers a View built
        # outside the middleware stack, as tests and the CLI do.
        self.request_id = getattr(request.state, "request_id", "") or uuid.uuid4().hex[:12]
        self._had_flashes = False
        #: Relation labels resolved during this request, keyed by resource
        #: then by primary key. Request-scoped on purpose: what a person may
        #: see is a policy decision made per request, so a longer-lived cache
        #: would have to re-check it on every read to stay correct.
        self.relation_labels: dict[str, dict[str, str]] = {}

    # -- shortcuts ----------------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self.state.settings

    @property
    def registry(self) -> Registry:
        return self.state.registry

    @property
    def templates(self) -> Templates:
        return self.state.templates

    @property
    def timezone(self) -> str:
        """The zone this request's output should be rendered in.

        The signed-in person's preference, falling back to a header the browser
        can set and then to the deployment default. Never the server's own
        zone, which is an accident of where it happens to run.
        """
        if self.identity.is_authenticated and self.identity.timezone:
            return self.identity.timezone
        supplied = self.request.headers.get("X-Timezone", "").strip()
        if supplied and is_valid_timezone(supplied):
            return supplied
        cookie = self.request.cookies.get("tz", "").strip()
        if cookie and is_valid_timezone(cookie):
            return cookie
        return self.settings.timezone

    @property
    def ctx(self) -> Ctx:
        """The context handed to providers."""
        extra: dict[str, Any] = {"ip": self.client_ip}
        # Present only when the deployment keeps it (CRM_OIDC_KEEP_ACCESS_TOKEN)
        # and the caller signed in through OIDC. A provider configured for
        # `caller_token` reads it here and falls back to its own credentials
        # when it is absent -- which is what makes the CLI and background jobs
        # keep working.
        if self.settings.oidc_keep_access_token:
            token = self.state.sessions.access_token(self.request)
            if token:
                extra["access_token"] = token
        return Ctx(
            identity=self.identity,
            request_id=self.request_id,
            timezone=self.timezone,
            extra=extra,
        )

    @property
    def client_ip(self) -> str:
        """The caller's address, for the audit log.

        A forwarded header is only believed when the deployment has said it
        trusts its proxy; otherwise any client could forge its own address into
        the audit trail.
        """
        if self.settings.proxy_trusted_ips:
            forwarded = self.request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        client = self.request.client
        return client.host if client else ""

    @property
    def wants_json(self) -> bool:
        return htmx.wants_json(self.request)

    @property
    def is_fragment(self) -> bool:
        return self.htmx.wants_fragment

    def resource(self, name: str) -> Resource:
        if not self.registry.has_resource(name):
            raise NotFound(f"There is no {name!r} here.")
        return self.registry.resource(name)

    # -- query parameters ---------------------------------------------------

    def param(self, name: str, default: str = "") -> str:
        return self.request.query_params.get(name, default)

    def int_param(self, name: str, default: int) -> int:
        raw = self.request.query_params.get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    def page_params(self, default_size: int | None = None) -> tuple[int, int]:
        """``(page, page_size)`` from the query string, bounded to safe values."""
        page = max(1, self.int_param("page", 1))
        size = clamp_page_size(
            self.int_param("page_size", 0) or None,
            default_size or self.settings.default_page_size,
        )
        return page, size

    # -- security -----------------------------------------------------------

    @property
    def session_id(self) -> str:
        """Binds CSRF tokens to the signed-in person."""
        return self.identity.subject if self.identity.is_authenticated else ""

    def csrf_token(self) -> str:
        return self.state.csrf.issue(self.session_id)

    async def check_csrf(self) -> None:
        await self.state.csrf.validate(self.request, self.session_id)

    def require_login(self) -> None:
        if not self.identity.is_authenticated:
            raise AuthenticationRequired()
        if self.identity.must_change_password:
            raise MustChangePassword()

    # -- rendering ----------------------------------------------------------

    def context(self, **extra: Any) -> dict[str, Any]:
        """The variables every template can rely on."""
        return {
            "request": self.request,
            "identity": self.identity,
            "user": self.identity,
            "menu": self.registry.menu(self.identity),
            "csrf_token": self.csrf_token(),
            "htmx": self.htmx,
            "is_fragment": self.is_fragment,
            "settings": self.settings,
            "registry": self.registry,
            "templates": self.templates,
            "current_path": self.request.url.path,
            # Read by the date/time filters. Presentation only: what is stored
            # is always UTC.
            "timezone": self.timezone,
            # The same thing in the form a field expects, for a computed value
            # that renders itself -- a shift naming itself by when it is has to
            # say so in the reader's zone, like every other instant on the page.
            "render_ctx": self.ctx,
            # Whether this application holds this person's password, so the
            # account menu can offer to change it only when changing it here
            # would do something. Under SSO it is None.
            "password_manager": self.password_manager,
            # Anything said by the request that redirected here.
            "flashes": self._take_flashes(),
            **extra,
        }

    def _take_flashes(self) -> list[Any]:
        """Messages left by an earlier request, read once."""
        messages = self.state.flash.read(self.request)
        self._had_flashes = bool(messages)
        return messages

    @property
    def password_manager(self) -> Any:
        """The auth provider owning the current identity's password, if any."""
        return self.state.auth.password_manager(self.identity)

    def render(
        self,
        template: str,
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        **context: Any,
    ) -> HTMLResponse:
        response = self.templates.response(
            template, self.context(**context), status_code=status_code, headers=headers
        )
        # Rendered means delivered: clearing here rather than on read is what
        # stops a message reappearing on the next page.
        if self._had_flashes:
            self.state.flash.clear(response)
        return response

    def render_view(
        self, resource: Resource, kind: str, *, status_code: int = 200, **context: Any
    ) -> HTMLResponse:
        """Render a resource view through the override chain.

        The toolbar above a list, a board, a calendar and a chart is the same
        toolbar, so what it needs is supplied here rather than by each handler
        -- one of them forgetting would silently drop the filter panel from
        that view alone. Only those views, though: a detail page renders no
        toolbar, and building its menus would mean resolving every filterable
        column's choices -- which may be a callable that goes to a backend --
        for markup nobody renders.
        """
        # A panel is this same view rendered inside a dashboard: the handler,
        # the query and the template are unchanged, only the page around them
        # goes away. Templates pick that up by extending `layout` rather than
        # base.html directly, so a view is embeddable without knowing it is.
        panel = self.param("panel") == "1"
        context.setdefault("panel", panel)
        context.setdefault("layout", "views/_bare.html" if panel else "base.html")

        if kind in TOOLBAR_VIEWS and not panel:
            from app.web.filters import active_filters, filter_schema

            context.setdefault("chips", active_filters(dict(self.request.query_params), resource))
            context.setdefault("filter_schema", filter_schema(resource, self.identity))

        template = self.templates.view_template(resource.name, kind)
        return self.render(
            template, status_code=status_code, resource=resource, view_kind=kind, **context
        )

    def json(self, payload: Any, *, status_code: int = 200) -> JSONResponse:
        return JSONResponse(_jsonable(payload), status_code=status_code)

    def redirect(self, url: str, *, status_code: int = 303) -> Response:
        """Navigate, honouring HTMX so a fragment request does not swap a page in."""
        if self.htmx.is_htmx:
            return htmx.redirect(HTMLResponse("", status_code=204), url)
        return RedirectResponse(url, status_code=status_code)

    def toast(self, response: Response, message: str, level: str = "success") -> Response:
        """Say something to the person, whichever way this response travels.

        HTMX can carry it in a header and show it without navigating. A plain
        form post cannot -- it answers with a redirect, and the browser drops
        the header along with the rest of that response -- so the message goes
        into a short-lived cookie for the next page instead. Getting this wrong
        is not a cosmetic bug: it makes a button that worked look like a button
        that did nothing.
        """
        if not message:
            return response
        if self.htmx.is_htmx:
            return htmx.toast(response, message, level)  # type: ignore[arg-type]
        self.state.flash.add(self.request, response, message, level)
        return response

    def url_for_record(self, resource: Resource, record: Record | Mapping[str, Any]) -> str:
        pk = record.get(resource.pk) if isinstance(record, Mapping) else record
        return f"/r/{resource.name}/{pk}"


def _jsonable(value: Any) -> Any:
    """Convert records, dates and decimals into something JSON can carry."""
    from datetime import date, datetime, time
    from decimal import Decimal

    if isinstance(value, Record):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, Mapping):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in vars(value).items() if not k.startswith("_")}
    return value


#: Paths reachable while an account is on a temporary password.
#:
#: The password page itself, obviously -- refusing access to the one screen
#: that fixes the problem would leave the person stuck. Signing out, so they
#: are not trapped. And the diagnostics, which have no data on them.
PASSWORD_CHANGE_ALLOWED = ("/account/password", "/logout", "/login", "/healthz", "/readyz")


async def build_view(request: Request) -> View:
    """FastAPI dependency: authenticate and assemble the request context.

    Also where the two checks that must not be forgettable happen. Both were
    once the handler's job, and the trouble with that is silent: a new route
    that omits the call is not a test failure, it is a hole nobody notices.
    Every route resolves this dependency, so putting them here means a route
    cannot opt out of them by being written carelessly.
    """
    state: AppState = request.app.state.crm
    identity = await state.auth.authenticate(request)
    view = View(request, state, identity)
    await view.check_csrf()
    _enforce_password_change(view)
    await _prepare_policies(view)
    return view


def _enforce_password_change(view: View) -> None:
    """Hold an account on a temporary password at the password page.

    Here rather than in ``require_login``, because not every route calls it --
    a list view checks ``require_can`` instead -- and a gate that some routes
    forget is not a gate. Every route goes through this dependency, so this is
    the one place that cannot be bypassed by adding a handler.
    """
    if not view.identity.must_change_password:
        return
    path = view.request.url.path
    if any(path.startswith(allowed) for allowed in PASSWORD_CHANGE_ALLOWED):
        return
    raise MustChangePassword()


async def _prepare_policies(view: View) -> None:
    """Load database-backed grants for whichever resource this URL names.

    Done here rather than in each handler on purpose. Policy methods are
    synchronous -- they are called from templates and from tight loops -- so
    their data has to be fetched once, before any of them run. Doing that from
    a dependency means no route can forget to, and a route that grows a second
    resource reference gets the same treatment for free.
    """
    from app.resources.rbac import DbPolicy, clear_request_grants

    clear_request_grants()

    name = view.request.path_params.get("resource_name")
    if not name or not view.registry.has_resource(str(name)):
        return

    resource = view.registry.resource(str(name))
    for candidate in _related_resources(view, resource):
        policy = candidate.policy
        if isinstance(policy, DbPolicy):
            await policy.prepare(view.identity, candidate.name, view.ctx)


def _related_resources(view: View, resource: Any) -> list[Any]:
    """The resource named by the URL, plus the ones a page will reach into.

    A detail page renders related lists and resolves relation labels, each of
    which consults another resource's policy. Preparing them together keeps
    that to one pass rather than a lookup mid-render.
    """
    found = [resource]
    seen = {resource.name}
    for field in (*resource.relations, *resource.backrefs):
        target = getattr(field, "target_resource", None) or getattr(
            getattr(field, "relation", None), "resource", None
        )
        if target and target not in seen and view.registry.has_resource(target):
            found.append(view.registry.resource(target))
            seen.add(target)
    return found
