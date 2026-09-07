"""A provider backed by an HTTP API.

The point of this one is that most APIs are not query engines. They return a
list of things, perhaps with a page parameter, and little else. Rather than
demand that every endpoint grow filtering and sorting, this provider declares
honestly what it can push down and lets the capability shim supply the rest.

The mapping is declarative, because no two APIs agree on anything: where the
rows live in the response, how pages are requested, what a total is called.
"""

from __future__ import annotations

import asyncio
import time as time_module
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal

import httpx

from app.core.connections import ConnectionSpec, register_connection
from app.core.errors import ConfigError, ConflictError, NotFound, ProviderError
from app.core.query import ListQuery, Op, Page
from app.core.registry import register_provider_factory
from app.core.results import Ctx, Record, WriteResult
from app.providers.base import BaseProvider, Capabilities

#: How an API asks for the next page.
Pagination = Literal["page", "offset", "cursor", "none"]


@dataclass(slots=True)
class Endpoint:
    """Where one operation lives, relative to the connection's base URL."""

    path: str = ""
    method: str = "GET"


@dataclass(slots=True)
class RestMapping:
    """How to talk to one collection.

    Every field here exists because some real API does it differently.
    """

    #: Path to the collection, e.g. "/v1/customers".
    path: str
    #: Dotted path to the array of rows in the response. Empty means the body
    #: itself is the array.
    items_key: str = "results"
    #: Dotted path to the total count, if the API reports one.
    total_key: str = "count"
    #: Dotted path to the next-page cursor, for cursor pagination.
    cursor_key: str = "next"

    pagination: Pagination = "page"
    page_param: str = "page"
    size_param: str = "page_size"
    offset_param: str = "offset"
    cursor_param: str = "cursor"

    #: Query parameter carrying a free-text search term, if supported.
    search_param: str = ""
    #: Query parameter carrying a sort key, if supported.
    sort_param: str = ""
    #: Template for a descending sort value, e.g. "-{field}".
    sort_desc_template: str = "-{field}"
    #: Maps our operators to query-parameter templates, e.g.
    #: ``{Op.EQ: "{field}", Op.GTE: "{field}__gte"}``. Only listed operators
    #: are pushed down; everything else is emulated.
    filter_params: dict[Op, str] = dc_field(default_factory=dict)

    #: Path to a single record. ``{pk}`` is substituted.
    detail_path: str = "{path}/{pk}"
    #: Extra query parameters sent on every request.
    static_params: dict[str, str] = dc_field(default_factory=dict)

    #: Wrap the body of a write, e.g. ``{"data": ...}``. Empty sends it bare.
    write_envelope: str = ""
    #: Where the created/updated record is in a write response.
    write_items_key: str = ""


def dig(payload: Any, path: str) -> Any:
    """Read a dotted path out of a nested response, tolerating absence."""
    if not path:
        return payload
    current = payload
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, Sequence) and not isinstance(current, str) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


class OAuth2ClientCredentials(httpx.Auth):
    """Bearer tokens fetched on demand and renewed before they lapse.

    The other auth types are a header computed once, when the connection opens.
    This one cannot be: a token expires, so it has to be fetched at first use
    and replaced on the way. That makes it the only auth type holding mutable
    state, and the reason for the lock -- a burst of requests against a cold
    connection must fetch one token between them, not one each.

    ``leeway`` is what stops a token being presented in the instant it expires:
    it is renewed early by that many seconds, which also absorbs a little clock
    skew between this process and the issuer.
    """

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str = "",
        scope: str = "",
        client_auth: str = "basic",
        extra: Mapping[str, Any] | None = None,
        timeout: float = 15.0,
        leeway: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not token_url or not client_id:
            raise ConfigError("oauth2 auth needs a token_url and a client_id")
        if client_auth not in ("basic", "body"):
            raise ConfigError(
                f"unknown client_auth {client_auth!r}; use 'basic' or 'body'"
            )
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self.client_auth = client_auth
        self.extra = dict(extra or {})
        self.timeout = timeout
        self.leeway = leeway
        self._transport = transport
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    # -- httpx integration --------------------------------------------------

    async def async_auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {await self._bearer()}"
        response = yield request

        if response.status_code == 401:
            # The token was refused before it looked expired -- revoked, or the
            # issuer disagrees about the clock. Fetch a fresh one and retry
            # once. Only once: a genuinely unauthorised client would otherwise
            # loop against the token endpoint.
            await self._fetch(force=True)
            request.headers["Authorization"] = f"Bearer {self._token}"
            yield request

    def sync_auth_flow(self, request):
        raise ProviderError("this connection is async-only; use an async client")

    # -- token lifecycle ----------------------------------------------------

    @property
    def _valid(self) -> bool:
        return bool(self._token) and time_module.monotonic() < self._expires_at

    async def _bearer(self) -> str:
        if not self._valid:
            await self._fetch()
        return self._token

    async def _fetch(self, *, force: bool = False) -> None:
        async with self._lock:
            # Another request may have renewed it while this one waited, which
            # is the whole point of taking the lock before looking again.
            if self._valid and not force:
                return

            data: dict[str, Any] = {"grant_type": "client_credentials", **self.extra}
            if self.scope:
                data["scope"] = self.scope
            # httpx distinguishes "no auth" from "use the client's default",
            # and only the latter is accepted per-request.
            auth: Any = httpx.USE_CLIENT_DEFAULT
            if self.client_auth == "basic":
                auth = (self.client_id, self.client_secret)
            else:
                data["client_id"] = self.client_id
                data["client_secret"] = self.client_secret

            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self._transport
            ) as client:
                try:
                    response = await client.post(self.token_url, data=data, auth=auth)
                except httpx.HTTPError as exc:
                    raise ProviderError(f"token request to {self.token_url!r} failed: {exc}") from exc

            if response.status_code >= 400:
                # The body carries the issuer's own reason -- invalid_scope,
                # unauthorized_client -- which is the only useful thing to say.
                raise ProviderError(
                    f"token request to {self.token_url!r} returned "
                    f"HTTP {response.status_code}: {response.text[:200]}"
                )
            try:
                body = response.json()
            except ValueError as exc:
                raise ProviderError(f"token endpoint {self.token_url!r} did not return JSON") from exc

            token = body.get("access_token")
            if not token:
                raise ProviderError(f"token endpoint {self.token_url!r} returned no access_token")
            self._token = str(token)

            try:
                lifetime = float(body.get("expires_in", 3600))
            except (TypeError, ValueError):
                lifetime = 3600.0
            self._expires_at = time_module.monotonic() + max(lifetime - self.leeway, 0.0)


class RestConnection:
    """A shared HTTP client for one API."""

    def __init__(
        self, client: httpx.AsyncClient, *, base_url: str = "", caller_token: bool = False
    ) -> None:
        self.client = client
        self.base_url = base_url
        #: Send the *caller's* own bearer token when the request has one.
        #: The client's configured credentials remain the fallback, so reads
        #: from a background job or the CLI still work.
        self.caller_token = caller_token

    async def close(self) -> None:
        await self.client.aclose()


@register_connection(
    "rest",
    close=lambda conn: conn.close(),
    check=lambda conn: _check_rest(conn),
)
async def open_rest(spec: ConnectionSpec) -> RestConnection:
    base_url = str(spec.option("base_url", required=True)).rstrip("/")
    headers = dict(spec.option("headers") or {})

    timeout = float(spec.option("timeout", 15.0))
    auth = spec.option("auth") or {}

    # "Act as whoever is signed in." The header cannot be built here -- it
    # differs per request -- so this arm only records the intent and unwraps
    # the fallback credentials, which is what a caller with no token uses.
    caller_token = auth.get("type") == "caller_token"
    if caller_token:
        auth = auth.get("fallback") or {}

    flow = _apply_auth(auth, headers, timeout, spec.name)

    client = httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        auth=flow,
        timeout=timeout,
        follow_redirects=True,
    )
    return RestConnection(client, base_url=base_url, caller_token=caller_token)


def _apply_auth(
    auth: dict[str, Any], headers: dict[str, str], timeout: float, name: str
) -> httpx.Auth | None:
    """Turn one auth block into headers, or into a flow that refreshes itself."""
    flow: httpx.Auth | None = None

    match auth.get("type"):
        case None | "" | "none":
            pass
        case "bearer":
            headers["Authorization"] = f"Bearer {auth['token']}"
        case "header":
            headers[auth["name"]] = auth["value"]
        case "basic":
            import base64

            raw = f"{auth['username']}:{auth['password']}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        case "oauth2":
            # The one arm that is not a header: see OAuth2ClientCredentials.
            flow = OAuth2ClientCredentials(
                token_url=auth.get("token_url", ""),
                client_id=auth.get("client_id", ""),
                client_secret=auth.get("client_secret", ""),
                scope=auth.get("scope", ""),
                client_auth=auth.get("client_auth", "basic"),
                extra=auth.get("extra"),
                timeout=timeout,
                leeway=float(auth.get("leeway", 30.0)),
            )
        case unknown:
            # Falling through would build an unauthenticated client and fail
            # later as a 401 from the API, which reads like the credentials
            # being wrong rather than the type name being misspelled.
            raise ConfigError(
                f"unknown auth type {unknown!r} for connection {name!r}; known "
                "types: bearer, header, basic, oauth2, caller_token"
            )

    return flow


async def _check_rest(conn: RestConnection) -> tuple[bool, str]:
    response = await conn.client.get("/")
    # Any answer proves reachability; a 404 at the root is normal and fine.
    return response.status_code < 500, f"{conn.base_url} -> HTTP {response.status_code}"


def _jsonable(value: Any) -> Any:
    """Serialise a canonical Python value for a JSON body.

    Fields hand over real ``date`` and ``Decimal`` objects; converting them is
    this provider's job, exactly as adapting them to column types is the SQL
    provider's.
    """
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


class RestProvider(BaseProvider):
    """Reads and writes one API collection."""

    def __init__(
        self,
        connection: RestConnection,
        mapping: RestMapping,
        *,
        name: str = "",
        pk_field: str = "id",
        searchable_fields: tuple[str, ...] = (),
        read_only: bool = True,
    ) -> None:
        self.connection = connection
        self.mapping = mapping
        self.name = name or f"rest:{mapping.path}"
        self.pk_field = pk_field
        self.capabilities = Capabilities(
            read=True,
            write=not read_only,
            delete=not read_only,
            # Only what the mapping actually declares. Claiming more would make
            # the shim step aside and quietly return wrong results.
            server_filter=bool(mapping.filter_params),
            filter_ops=frozenset(mapping.filter_params) if mapping.filter_params else frozenset(),
            server_sort=bool(mapping.sort_param),
            server_search=bool(mapping.search_param),
            server_paginate=mapping.pagination != "none",
            total_count=bool(mapping.total_key),
            aggregate=False,
            searchable_fields=searchable_fields,
        )

    # -- request building ---------------------------------------------------

    def _params(self, q: ListQuery) -> dict[str, Any]:
        params: dict[str, Any] = dict(self.mapping.static_params)
        m = self.mapping

        if m.search_param and q.search:
            params[m.search_param] = q.search

        if m.sort_param and q.sort:
            first = q.sort[0]
            params[m.sort_param] = (
                m.sort_desc_template.format(field=first.field)
                if first.dir.value == "desc"
                else first.field
            )

        match m.pagination:
            case "page":
                params[m.page_param] = q.page
                params[m.size_param] = q.page_size
            case "offset":
                params[m.offset_param] = q.offset
                params[m.size_param] = q.page_size
            case "cursor":
                params[m.size_param] = q.page_size

        params.update(self._filter_params(q))
        return params

    def _filter_params(self, q: ListQuery) -> dict[str, Any]:
        """Translate the conditions this API understands into query parameters.

        Only flat AND-ed conditions can be expressed as query parameters. The
        shim decides whether to push down at all, so anything reaching here is
        already known to be supported.
        """
        from app.core.query import iter_conditions

        params: dict[str, Any] = {}
        for condition in iter_conditions(q.effective_filter):
            template = self.mapping.filter_params.get(condition.op)
            if not template:
                continue
            key = template.format(field=condition.field)
            value = condition.value
            if isinstance(value, (list, tuple, set)):
                value = ",".join(str(_jsonable(v)) for v in value)
            params[key] = _jsonable(value)
        return params

    def _caller_headers(self, ctx: Ctx | None) -> dict[str, str]:
        """The acting user's bearer, when the connection asked to act as them.

        Absent for a background job, the CLI, or a caller who signed in some
        other way -- and absent means "use the client's own credentials",
        which is why those keep working rather than starting to fail with a
        401 the day this is switched on.
        """
        if not getattr(self.connection, "caller_token", False) or ctx is None:
            return {}
        token = (ctx.extra or {}).get("access_token")
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _get(
        self, path: str, params: dict[str, Any] | None = None, ctx: Ctx | None = None
    ) -> Any:
        try:
            response = await self.connection.client.get(
                path, params=params or {}, headers=self._caller_headers(ctx)
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: request failed: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ProviderError(
                f"{self.name}: HTTP {response.status_code} from {response.request.url}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(f"{self.name}: response was not JSON") from exc

    # -- reads --------------------------------------------------------------

    async def list(self, q: ListQuery, ctx: Ctx) -> Page[Record]:
        payload = await self._get(self.mapping.path, self._params(q), ctx)
        if payload is None:
            return Page.empty(q)

        raw = dig(payload, self.mapping.items_key)
        if raw is None and isinstance(payload, list):
            raw = payload
        if not isinstance(raw, list):
            raise ProviderError(
                f"{self.name}: expected a list at {self.mapping.items_key!r} in the response"
            )

        total = dig(payload, self.mapping.total_key) if self.mapping.total_key else None
        cursor = dig(payload, self.mapping.cursor_key) if self.mapping.cursor_key else None

        items = self._records([dict(row) for row in raw if isinstance(row, Mapping)])
        return Page(
            items=items,
            page=q.page,
            page_size=q.page_size,
            total=int(total) if isinstance(total, (int, float)) else None,
            has_more=bool(cursor) if cursor is not None else len(items) >= q.page_size,
            cursor=str(cursor) if cursor else None,
        )

    async def get(self, pk: Any, ctx: Ctx) -> Record | None:
        path = self.mapping.detail_path.format(path=self.mapping.path, pk=pk)
        payload = await self._get(path, dict(self.mapping.static_params), ctx)
        if payload is None:
            return None
        body = dig(payload, self.mapping.write_items_key) if self.mapping.write_items_key else payload
        return self._record(dict(body)) if isinstance(body, Mapping) else None

    # -- writes -------------------------------------------------------------

    async def create(self, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        return await self._write("POST", self.mapping.path, data, ctx)

    async def update(self, pk: Any, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        path = self.mapping.detail_path.format(path=self.mapping.path, pk=pk)
        return await self._write("PATCH", path, data, ctx)

    async def delete(self, pk: Any, ctx: Ctx) -> WriteResult:
        path = self.mapping.detail_path.format(path=self.mapping.path, pk=pk)
        try:
            response = await self.connection.client.request(
                "DELETE", path, headers=self._caller_headers(ctx)
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: delete failed: {exc}") from exc
        if response.status_code == 404:
            raise NotFound(f"no record with {self.pk_field}={pk!r}")
        if response.status_code >= 400:
            return WriteResult.failure(f"The API refused the deletion (HTTP {response.status_code}).")
        return WriteResult.success()

    async def _write(
        self, method: str, path: str, data: dict[str, Any], ctx: Ctx | None = None
    ) -> WriteResult:
        body: Any = {k: _jsonable(v) for k, v in data.items()}
        if self.mapping.write_envelope:
            body = {self.mapping.write_envelope: body}
        try:
            response = await self.connection.client.request(
                method, path, json=body, headers=self._caller_headers(ctx)
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: write failed: {exc}") from exc

        if response.status_code == 404:
            raise NotFound("that record no longer exists")
        if response.status_code == 409:
            raise ConflictError("The API reported a conflict with existing data.")
        if response.status_code == 422:
            return WriteResult.invalid(_field_errors(response))
        if response.status_code == 202:
            # Accepted but not applied. Reporting success here would tell the
            # user a change landed when the API only promised to consider it.
            return WriteResult.pending(message="The API accepted the change for processing.")
        if response.status_code >= 400:
            return WriteResult.failure(f"The API refused the change (HTTP {response.status_code}).")

        try:
            payload = response.json()
        except ValueError:
            return WriteResult.success()
        body_out = dig(payload, self.mapping.write_items_key) if self.mapping.write_items_key else payload
        record = self._record(dict(body_out)) if isinstance(body_out, Mapping) else None
        return WriteResult.success(record)

    async def health(self) -> tuple[bool, str]:
        try:
            page = await self.list(ListQuery(page_size=1, with_total=False), Ctx.system())
            return True, f"{self.mapping.path}: reachable ({len(page.items)} sample row)"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"


def _field_errors(response: httpx.Response) -> dict[str, str]:
    """Best-effort extraction of per-field messages from a 422 body."""
    try:
        payload = response.json()
    except ValueError:
        return {}
    if isinstance(payload, Mapping):
        for key in ("errors", "detail", "fields"):
            candidate = payload.get(key, payload if key == "errors" else None)
            if isinstance(candidate, Mapping):
                return {str(k): _first_message(v) for k, v in candidate.items()}
    return {}


def _first_message(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and value:
        return str(value[0])
    return str(value)


@register_provider_factory("rest")
def build_rest_provider(handle: RestConnection, target: str, resource) -> RestProvider:
    """Wire ``api.name#/path`` to a collection.

    Mapping details come from the resource's ``rest`` option so they stay next
    to the declaration they describe.
    """
    options = dict(getattr(resource, "rest_mapping", {}) or {})
    options.setdefault("path", target if target.startswith("/") else f"/{target}")
    mapping = RestMapping(**options)
    return RestProvider(
        handle,
        mapping,
        name=f"rest:{mapping.path}",
        pk_field=resource.pk,
        searchable_fields=resource.searchable_fields(),
        read_only=not getattr(resource, "rest_writable", False),
    )
