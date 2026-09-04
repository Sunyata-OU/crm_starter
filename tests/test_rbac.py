"""Database-driven access control.

The point of this module is that an administrator can change who may do what
without a deployment. These tests pin down the parts that make that safe: an
empty table must not lock everyone out, roles must add access rather than
subtract it, and a grant must hold on every path -- not only on lists.
"""

from __future__ import annotations

import pytest

from app.core.results import Ctx, Identity, Record
from app.providers.memory import MemoryProvider
from app.resources.policy import RolePolicy
from app.resources.rbac import (
    ANY_RESOURCE,
    DEFAULT_GRANTS,
    DbPolicy,
    Grant,
    GrantTable,
    PermissionStore,
    clear_request_grants,
    store,
)

CTX = Ctx.system()

ADMIN = Identity(subject="a", email="admin@x.test", roles=frozenset({"admin"}))
REP = Identity(subject="k", email="kim@x.test", roles=frozenset({"user"}))
VIEWER = Identity(subject="v", email="dana@x.test", roles=frozenset({"readonly"}))
NOBODY = Identity(subject="n", email="no@x.test", roles=frozenset())


def grant_rows(*rows: dict) -> MemoryProvider:
    return MemoryProvider([{"id": i, **r} for i, r in enumerate(rows, 1)])


@pytest.fixture(autouse=True)
def clean_request():
    """Grants are request-scoped; do not let one test see another's."""
    clear_request_grants()
    yield
    clear_request_grants()
    store.provider = None
    store.invalidate()


async def policy_for(rows, resource_name="deals", identity=REP, **kwargs) -> DbPolicy:
    """A prepared policy, as a request would have it."""
    store.bind(grant_rows(*rows))
    policy = DbPolicy(**kwargs)
    policy.resource_name = resource_name
    await policy.prepare(identity, resource_name, CTX)
    return policy


class TestGrantParsing:
    def test_flags_come_from_the_row(self):
        grant = Grant.from_record(Record({"role": "user", "resource": "deals",
                                          "can_read": True, "can_delete": False}))
        assert grant.read and not grant.delete

    def test_flags_stored_as_text_are_understood(self):
        # SQLite has no boolean type; some drivers hand back 1/0 or "true".
        grant = Grant.from_record(Record({"role": "u", "can_read": 1, "can_update": "true"}))
        assert grant.read and grant.update

    def test_field_lists_accept_json_or_csv(self):
        json_form = Grant.from_record(Record({"role": "u", "hidden_fields": '["a","b"]'}))
        csv_form = Grant.from_record(Record({"role": "u", "hidden_fields": "a, b"}))
        assert json_form.hidden_fields == csv_form.hidden_fields == frozenset({"a", "b"})

    def test_an_unknown_row_scope_falls_back_to_all(self):
        assert Grant.from_record(Record({"role": "u", "row_scope": "nonsense"})).row_scope == "all"


class TestGrantTable:
    def test_wildcard_and_specific_grants_both_apply(self):
        table = GrantTable.from_records([
            Record({"role": "user", "resource": ANY_RESOURCE, "can_read": True}),
            Record({"role": "user", "resource": "deals", "can_read": True, "can_update": True}),
        ])
        assert len(table.for_identity(REP, "deals")) == 2

    def test_an_unrelated_resource_grant_does_not_apply(self):
        table = GrantTable.from_records([
            Record({"role": "user", "resource": "invoices", "can_read": True}),
        ])
        assert table.for_identity(REP, "deals") == []

    def test_grants_for_roles_the_caller_lacks_are_ignored(self):
        table = GrantTable.from_records([
            Record({"role": "manager", "resource": "deals", "can_read": True}),
        ])
        assert table.for_identity(REP, "deals") == []


class TestOperations:
    async def test_a_granted_operation_is_allowed(self):
        policy = await policy_for([
            {"role": "user", "resource": "deals", "can_read": True, "can_update": True},
        ])
        assert policy.allows("read", REP)
        assert policy.allows("update", REP)

    async def test_an_ungranted_operation_is_refused(self):
        policy = await policy_for([
            {"role": "user", "resource": "deals", "can_read": True},
        ])
        assert not policy.allows("delete", REP)
        assert not policy.allows("create", REP)

    async def test_no_grant_at_all_means_no_access(self):
        policy = await policy_for([
            {"role": "manager", "resource": "deals", "can_read": True},
        ])
        assert not policy.allows("read", REP)

    async def test_an_administrator_bypasses_the_table(self):
        policy = await policy_for([], identity=ADMIN)
        assert policy.allows("delete", ADMIN)

    async def test_an_anonymous_caller_is_always_refused(self):
        from app.core.results import ANONYMOUS

        policy = await policy_for([{"role": "user", "resource": "deals", "can_read": True}])
        assert not policy.allows("read", ANONYMOUS)


class TestRolesAddAccess:
    """Holding a second role must never take access away."""

    async def test_the_widest_scope_wins(self):
        both = Identity(subject="b", email="b@x.test", roles=frozenset({"user", "manager"}))
        policy = await policy_for(
            [
                {"role": "user", "resource": "deals", "can_read": True, "row_scope": "own"},
                {"role": "manager", "resource": "deals", "can_read": True, "row_scope": "all"},
            ],
            identity=both,
        )
        assert policy.scope(both) is None, "the broader role must win"

    async def test_a_field_hidden_by_only_one_role_stays_visible(self):
        both = Identity(subject="b", email="b@x.test", roles=frozenset({"user", "manager"}))
        policy = await policy_for(
            [
                {"role": "user", "resource": "deals", "can_read": True,
                 "hidden_fields": '["margin"]'},
                {"role": "manager", "resource": "deals", "can_read": True},
            ],
            identity=both,
        )
        assert policy._hidden(both) == frozenset()


class TestRowScope:
    async def test_all_returns_no_restriction(self):
        policy = await policy_for([
            {"role": "user", "resource": "deals", "can_read": True, "row_scope": "all"},
        ])
        assert policy.scope(REP) is None

    async def test_own_restricts_to_the_owner_column(self):
        policy = await policy_for(
            [{"role": "user", "resource": "deals", "can_read": True, "row_scope": "own"}],
            owner_field="owner",
            identity_attr="email",
        )
        condition = policy.scope(REP)
        assert condition is not None
        assert condition.field == "owner" and condition.value == "kim@x.test"

    async def test_none_denies_every_row(self):
        from app.resources.rbac import DENY_ALL

        policy = await policy_for([
            {"role": "user", "resource": "deals", "can_read": True, "row_scope": "none"},
        ])
        assert policy.scope(REP) == DENY_ALL

    async def test_no_grant_denies_every_row(self):
        policy = await policy_for([{"role": "other", "resource": "deals", "can_read": True}])
        from app.resources.rbac import DENY_ALL

        assert policy.scope(REP) == DENY_ALL

    async def test_own_scope_also_guards_a_direct_write(self):
        # scope() narrows lists, but an update names its own record and would
        # otherwise slip past.
        policy = await policy_for(
            [{"role": "user", "resource": "deals", "can_read": True,
              "can_update": True, "row_scope": "own"}],
            owner_field="owner", identity_attr="email",
        )
        mine = Record({"id": 1, "owner": "kim@x.test"})
        theirs = Record({"id": 2, "owner": "sam@x.test"})
        assert policy.allows("update", REP, mine)
        assert not policy.allows("update", REP, theirs)


class TestEmptyTableIsSafe:
    """An unconfigured deployment must behave as it did before RBAC existed."""

    async def test_an_unbound_store_falls_back_to_the_base_policy(self):
        store.provider = None
        store.invalidate()
        policy = DbPolicy(base=RolePolicy(read=["user"], write=["manager"]))
        policy.resource_name = "deals"
        assert policy.allows("read", REP), "the base policy should still grant this"
        assert not policy.allows("update", REP)

    async def test_the_base_scope_is_used_when_unconfigured(self):
        from app.resources.policy import OwnerPolicy

        store.provider = None
        store.invalidate()
        policy = DbPolicy(base=OwnerPolicy("owner", identity_attr="email"))
        policy.resource_name = "deals"
        condition = policy.scope(REP)
        assert condition is not None and condition.value == "kim@x.test"


class TestFieldRestrictions:
    async def test_hidden_fields_are_removed_from_the_readable_set(self):
        from app.fields.types import CurrencyField, TextField
        from app.resources.resource import Resource

        policy = await policy_for([
            {"role": "user", "resource": "deals", "can_read": True,
             "hidden_fields": '["margin"]'},
        ])
        resource = Resource("deals", provider=MemoryProvider([]),
                            fields=[TextField("name"), CurrencyField("margin")])
        assert "margin" not in policy.readable_fields(REP, resource)
        assert "name" in policy.readable_fields(REP, resource)

    async def test_readonly_fields_are_removed_from_the_writable_set(self):
        from app.fields.types import TextField
        from app.resources.resource import Resource

        policy = await policy_for([
            {"role": "user", "resource": "deals", "can_read": True, "can_update": True,
             "readonly_fields": '["stage"]'},
        ])
        resource = Resource("deals", provider=MemoryProvider([]),
                            fields=[TextField("name"), TextField("stage")])
        assert "stage" not in policy.writable_fields(REP, resource)
        assert "stage" in policy.readable_fields(REP, resource), "still visible, just locked"

    async def test_an_administrator_sees_every_field(self):
        from app.fields.types import TextField
        from app.resources.resource import Resource

        policy = await policy_for(
            [{"role": "admin", "resource": ANY_RESOURCE, "can_read": True,
              "hidden_fields": '["margin"]'}],
            identity=ADMIN,
        )
        resource = Resource("deals", provider=MemoryProvider([]),
                            fields=[TextField("name"), TextField("margin")])
        assert "margin" in policy.readable_fields(ADMIN, resource)


class TestCaching:
    async def test_the_table_is_cached_between_lookups(self):
        provider = grant_rows({"role": "user", "resource": "deals", "can_read": True})
        calls = []
        original = provider.list

        async def counting(q, ctx):
            calls.append(q)
            return await original(q, ctx)

        provider.list = counting  # type: ignore[method-assign]
        local = PermissionStore(provider)
        await local.table(CTX)
        await local.table(CTX)
        assert len(calls) == 1, "a second lookup must not re-query"

    async def test_invalidation_forces_a_reload(self):
        provider = grant_rows({"role": "user", "resource": "deals", "can_read": True})
        calls = []
        original = provider.list

        async def counting(q, ctx):
            calls.append(q)
            return await original(q, ctx)

        provider.list = counting  # type: ignore[method-assign]
        local = PermissionStore(provider)
        await local.table(CTX)
        local.invalidate()
        await local.table(CTX)
        assert len(calls) == 2

    async def test_the_cache_expires_on_its_own(self):
        # A grant changed by another worker, or by hand in SQL, must not be
        # ignored indefinitely.
        local = PermissionStore(grant_rows({"role": "user", "can_read": True}), ttl=0)
        first = await local.table(CTX)
        second = await local.table(CTX)
        assert first is not second


class TestShippedDefaults:
    def test_every_default_grant_has_the_same_columns(self):
        # A bulk insert compiles one statement for the batch, so a row missing
        # a key fails the whole seed.
        keys = {frozenset(g) for g in DEFAULT_GRANTS}
        assert len(keys) == 1

    def test_the_administrator_grant_covers_everything(self):
        admin = next(g for g in DEFAULT_GRANTS if g["role"] == "admin")
        assert admin["resource"] == ANY_RESOURCE
        assert all(admin[f"can_{op}"] for op in ("read", "create", "update", "delete"))

    def test_the_read_only_role_cannot_write(self):
        viewer = next(g for g in DEFAULT_GRANTS if g["role"] == "readonly")
        assert viewer["can_read"]
        assert not any(viewer[f"can_{op}"] for op in ("create", "update", "delete"))

    def test_a_rep_sees_only_their_own_deals(self):
        deals = next(
            g for g in DEFAULT_GRANTS if g["role"] == "user" and g["resource"] == "deals"
        )
        assert deals["row_scope"] == "own"
        assert not deals["can_delete"]
