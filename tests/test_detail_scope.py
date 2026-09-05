"""Detail reads fold the row scope into the query instead of checking after.

Fetching by key and testing the scope afterwards assumes the key selects at
most one row. A scope is exactly what makes that assumption worth doubting: on
a versioned table one business key matches several rows, the keyed read returns
whichever the backend orders first -- often a superseded one -- and that row
then fails the scope test, so a record the list had just shown 404s when opened.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.core.query import Op, iter_conditions
from app.fields.types import TextField
from app.main import create_app
from app.providers.memory import MemoryProvider
from app.resources.policy import OwnerPolicy
from app.resources.resource import Resource

from .conftest import build_registry, sign_in

#: One business key, two versions, different owners. The superseded row comes
#: back first, which is what made the old fetch-then-check order fail.
VERSIONED = [
    {"id": 251, "version": 1, "name": "Acme (superseded)", "owner": "other@example.com"},
    {"id": 251, "version": 2, "name": "Acme (current)", "owner": "kim@example.com"},
]


class VersionedProvider(MemoryProvider):
    """A provider whose key selects more than one row.

    MemoryProvider stores rows in a dict keyed by pk, so it cannot hold a
    second version of anything -- which is precisely the shape under test. This
    keeps the rows in a list instead and returns the first match from `get`,
    the way a real backend answers `SELECT ... WHERE id = ? LIMIT 1` against a
    versioned table with no ordering of its own.
    """

    def __init__(self, rows, **kw):
        super().__init__((), **kw)
        self._ordered = [dict(r) for r in rows]

    async def list(self, q, ctx):
        from app.providers import local

        return local.run_query(
            [self._record(dict(r)) for r in self._ordered], q,
            search_fields=self._search_fields,
        )

    async def get(self, pk, ctx):
        for row in self._ordered:
            if str(row.get(self.pk_field)) == str(pk):
                return self._record(dict(row))
        return None


@pytest.fixture
def versioned_client(settings):
    registry = build_registry()
    registry.add_resource(
        Resource(
            "versioned",
            provider=VersionedProvider(VERSIONED),
            policy=OwnerPolicy("owner", identity_attr="email", bypass_roles=()),
            fields=[
                TextField("id", in_form=False),
                TextField("version"),
                TextField("name"),
                TextField("owner"),
            ],
        )
    )
    with TestClient(create_app(settings=settings, registry=registry)) as c:
        sign_in(c, email="kim@example.com", roles=["user"])
        yield c


def load_queries(seen, resource_pk: str):
    """The queries `_load` itself issued, told apart from relation lookups.

    A detail page also lists -- to resolve relation labels and backrefs -- so a
    test that counted every list call would be asserting about the page, not
    about the read under test.
    """
    found = []
    for q in seen:
        conditions = list(iter_conditions(q.filter))
        if len(conditions) == 1 and conditions[0].op is Op.EQ and conditions[0].field == resource_pk:
            found.append(q)
    return found


@pytest.fixture
def spy_on_list(monkeypatch):
    import app.providers.memory as memory

    seen = []
    original = memory.MemoryProvider.list

    async def spy(self, q, ctx):
        seen.append(q)
        return await original(self, q, ctx)

    monkeypatch.setattr(memory.MemoryProvider, "list", spy)
    return seen


class TestANonUniqueKey:
    def test_the_caller_reaches_their_own_version(self, versioned_client):
        # The keyed read returns the superseded row, owned by someone else.
        # Checking the scope after that fetch 404s a record the list just showed.
        response = versioned_client.get("/r/versioned/251")
        assert response.status_code == 200
        assert "Acme (current)" in response.text

    def test_the_keyed_read_really_does_return_the_wrong_row(self, versioned_client):
        # Guards the fixture itself: if `get` stopped returning the superseded
        # version first, the test above would pass for the wrong reason.
        import asyncio

        provider = versioned_client.app.state.crm.registry.resource("versioned").provider
        row = asyncio.run(provider.get("251", None))
        assert row["name"] == "Acme (superseded)"


class TestScopeRemainsEnforced:
    def test_a_record_outside_the_scope_reads_as_absent(self, rep):
        # Deal 2 belongs to sam; kim is scoped away from it.
        assert rep.get("/r/deals/2").status_code == 404

    def test_a_key_that_does_not_exist_reads_identically(self, rep):
        # Routing both cases through one query collapsed them into one answer.
        # They have to stay indistinguishable, or the 404 confirms which keys
        # exist to someone who may not list them.
        missing = rep.get("/r/deals/9999")
        forbidden = rep.get("/r/deals/2")
        assert missing.status_code == forbidden.status_code == 404
        assert missing.text == forbidden.text

    def test_the_caller_still_reaches_their_own(self, rep):
        assert rep.get("/r/deals/1").status_code == 200


class TestHowTheReadIsIssued:
    def test_an_unscoped_read_stays_a_plain_keyed_get(self, admin, spy_on_list):
        # Nothing to fold in, so nothing to pay for: several providers answer a
        # keyed read without building a query at all.
        assert admin.get("/r/contacts/1").status_code == 200
        assert load_queries(spy_on_list, "id") == []

    def test_a_scoped_read_carries_the_key_and_the_scope_together(self, rep, spy_on_list):
        rep.get("/r/deals/1")
        [query] = load_queries(spy_on_list, "id")
        assert query.scope is not None, "the scope must ride along, not replace the key"
        assert query.effective_filter is not None
