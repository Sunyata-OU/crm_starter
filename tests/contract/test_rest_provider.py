"""The REST provider against the shared contract.

Served by an in-process fake API deliberately built to be unhelpful: it returns
rows and nothing else -- no filtering, no sorting, no total. That is the common
case for a third-party endpoint, and the point is that the same views work over
it because the shim supplies what the API does not.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from app.core.query import Condition, ListQuery, Op, Sort, SortDir
from app.core.results import Ctx
from app.providers.rest import RestConnection, RestMapping, RestProvider, dig
from app.providers.shim import CapabilityShim

from .conftest import SEARCH_FIELDS

CTX = Ctx.system()


def _encode(rows):
    return [
        {**r, "closed": r["closed"].isoformat() if isinstance(r["closed"], date) else r["closed"]}
        for r in rows
    ]


class FakeAPI:
    """A deliberately minimal endpoint: hands over rows, understands nothing."""

    def __init__(self, rows):
        self.rows = {r["id"]: dict(r) for r in _encode(rows)}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path == "/records":
            if request.method == "POST":
                body = json.loads(request.content)
                new_id = max(self.rows) + 1
                body["id"] = new_id
                self.rows[new_id] = body
                return httpx.Response(201, json=body)
            return httpx.Response(200, json={"results": list(self.rows.values())})

        if path.startswith("/records/"):
            pk = int(path.rsplit("/", 1)[-1])
            if request.method == "GET":
                row = self.rows.get(pk)
                return httpx.Response(200, json=row) if row else httpx.Response(404)
            if request.method == "PATCH":
                if pk not in self.rows:
                    return httpx.Response(404)
                self.rows[pk].update(json.loads(request.content))
                return httpx.Response(200, json=self.rows[pk])
            if request.method == "DELETE":
                if pk not in self.rows:
                    return httpx.Response(404)
                del self.rows[pk]
                return httpx.Response(204)

        return httpx.Response(404)


@pytest.fixture
def api(rows) -> FakeAPI:
    return FakeAPI(rows)


@pytest.fixture
def provider(api):
    """A read/write REST provider, wrapped so the full contract is available."""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(api.handler), base_url="https://api.test"
    )
    inner = RestProvider(
        RestConnection(client, base_url="https://api.test"),
        # No filter params, no sort param, no total: the endpoint does nothing
        # but return rows.
        RestMapping(path="/records", items_key="results", total_key="", pagination="none"),
        read_only=False,
    )
    return CapabilityShim(inner, search_fields=SEARCH_FIELDS)


class TestDig:
    def test_reads_a_nested_path(self):
        assert dig({"a": {"b": [1, 2]}}, "a.b.1") == 2

    def test_missing_path_is_none_not_an_error(self):
        assert dig({"a": {}}, "a.b.c") is None

    def test_empty_path_returns_the_payload(self):
        assert dig([1, 2], "") == [1, 2]


class TestCapabilityHonesty:
    """A provider must not claim more than its mapping supports."""

    def test_a_bare_endpoint_advertises_nothing_server_side(self, api):
        client = httpx.AsyncClient(transport=httpx.MockTransport(api.handler), base_url="https://api.test")
        inner = RestProvider(
            RestConnection(client),
            RestMapping(path="/records", total_key="", pagination="none"),
        )
        caps = inner.capabilities
        assert not caps.server_filter
        assert not caps.server_sort
        assert not caps.total_count

    def test_declared_filter_operators_are_advertised(self, api):
        client = httpx.AsyncClient(transport=httpx.MockTransport(api.handler), base_url="https://api.test")
        inner = RestProvider(
            RestConnection(client),
            RestMapping(
                path="/records",
                filter_params={Op.EQ: "{field}", Op.GTE: "{field}__gte"},
            ),
        )
        assert inner.capabilities.server_filter
        assert inner.capabilities.filter_ops == frozenset({Op.EQ, Op.GTE})
        # An operator the API does not understand must not be claimed.
        assert not inner.capabilities.supports_ops(frozenset({Op.ICONTAINS}))


class TestTheContractHolds:
    """The same behaviour as SQL, over an endpoint that can do none of it."""

    async def test_lists(self, provider):
        page = await provider.list(ListQuery(page_size=50), CTX)
        assert page.total == 7

    async def test_filters(self, provider):
        page = await provider.list(
            ListQuery(filter=Condition("stage", Op.EQ, "won"), page_size=50), CTX
        )
        assert len(page.items) == 3

    async def test_text_operators(self, provider):
        page = await provider.list(
            ListQuery(filter=Condition("name", Op.ICONTAINS, "lovelace"), page_size=50), CTX
        )
        assert [r["name"] for r in page.items] == ["Ada Lovelace"]

    async def test_sorts(self, provider):
        page = await provider.list(
            ListQuery(sort=(Sort("amount", SortDir.DESC),), page_size=50), CTX
        )
        assert [r["amount"] for r in page.items][0] == 9900

    async def test_paginates(self, provider):
        page = await provider.list(ListQuery(sort=(Sort("id"),), page=2, page_size=3), CTX)
        assert [r.pk for r in page.items] == [4, 5, 6]
        assert page.total == 7

    async def test_searches(self, provider):
        page = await provider.list(ListQuery(search="bletchley", page_size=50), CTX)
        assert [r["name"] for r in page.items] == ["Alan Turing"]

    async def test_applies_scope(self, provider):
        page = await provider.list(
            ListQuery(scope=Condition("owner", Op.EQ, "sam"), page_size=50), CTX
        )
        assert {r["owner"] for r in page.items} == {"sam"}

    async def test_aggregates(self, provider):
        from app.core.query import Agg, AggSpec, Measure

        rows = await provider.aggregate(
            AggSpec(group_by=("stage",), measures=(Measure(Agg.COUNT, alias="n"),)), CTX
        )
        assert {r["stage"]: r["n"] for r in rows} == {"won": 3, "open": 3, "lost": 1}

    async def test_gets_one(self, provider):
        record = await provider.get(1, CTX)
        assert record is not None and record["name"] == "Ada Lovelace"

    async def test_missing_is_none(self, provider):
        assert await provider.get(999, CTX) is None

    async def test_creates(self, provider):
        result = await provider.create({"name": "Sophie Wilson", "amount": 400}, CTX)
        assert result.ok and result.record["name"] == "Sophie Wilson"

    async def test_updates(self, provider):
        result = await provider.update(3, {"stage": "won"}, CTX)
        assert result.ok
        assert (await provider.get(3, CTX))["stage"] == "won"

    async def test_deletes(self, provider):
        assert (await provider.delete(5, CTX)).ok
        assert await provider.get(5, CTX) is None

    async def test_missing_update_raises_not_found(self, provider):
        from app.core.errors import NotFound

        with pytest.raises(NotFound):
            await provider.update(999, {"stage": "won"}, CTX)


class TestAcceptedButNotApplied:
    """An API returning 202 has promised nothing; say so rather than claim success."""

    async def test_202_becomes_a_pending_result(self, rows):
        def handler(request):
            return httpx.Response(202, json={"queued": True})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.test")
        provider = RestProvider(
            RestConnection(client), RestMapping(path="/records"), read_only=False
        )
        result = await provider.create({"name": "x"}, CTX)
        assert result.status.value == "pending"
        assert result.accepted and not result.ok
        assert result.correlation_id


class TestValueSerialisation:
    async def test_dates_are_sent_as_iso_strings(self, api):
        # Fields hand over real date objects; converting them for the wire is
        # this provider's job, not the field's.
        client = httpx.AsyncClient(transport=httpx.MockTransport(api.handler), base_url="https://api.test")
        provider = RestProvider(RestConnection(client), RestMapping(path="/records"), read_only=False)
        await provider.create({"name": "x", "closed": date(2026, 5, 4)}, CTX)
        sent = json.loads(api.requests[-1].content)
        assert sent["closed"] == "2026-05-04"
