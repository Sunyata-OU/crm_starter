"""Database-driven permissions, exercised through the real routes.

The unit tests in ``test_rbac.py`` check the policy object. These check that a
row in the permissions table actually governs what a request can do -- through
the dependency that prepares the grants, the routes that consult the policy,
and the templates that decide which buttons to draw.
"""

from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from app.core.registry import Registry
from app.fields.types import CurrencyField, StatusField, TextField
from app.main import create_app
from app.providers.memory import MemoryProvider
from app.resources.policy import RolePolicy
from app.resources.rbac import ANY_RESOURCE, DbPolicy, store
from app.resources.resource import Resource
from app.resources.views import Column, FormView, ListView, Section
from app.settings import Settings

from .conftest import csrf_from, sign_in


def await_sync(coro):
    """Run one coroutine from a synchronous test.

    The TestClient runs the application in its own loop; a fixture that needs
    to reach a provider directly cannot simply await.
    """
    import asyncio

    return asyncio.run(coro)

DEAL_ROWS = [
    {"id": 1, "name": "Own deal", "amount": 100, "stage": "open", "owner": "kim@x.test"},
    {"id": 2, "name": "Their deal", "amount": 200, "stage": "open", "owner": "sam@x.test"},
    {"id": 3, "name": "Another of theirs", "amount": 300, "stage": "won", "owner": "sam@x.test"},
]


def build_app(grants: list[dict]) -> TestClient:
    """An application whose deals resource is governed by ``grants``."""
    registry = Registry()

    registry.add_resource(
        Resource(
            "permissions",
            provider=MemoryProvider([{"id": i, **g} for i, g in enumerate(grants, 1)]),
            policy=RolePolicy(read=["admin"], write=["admin"]),
            in_menu=False,
            fields=[TextField("id"), TextField("role"), TextField("resource")],
        )
    )
    registry.add_resource(
        Resource(
            "deals",
            provider=MemoryProvider(DEAL_ROWS, searchable_fields=("name",)),
            policy=DbPolicy(owner_field="owner", identity_attr="email"),
            audited=False,
            fields=[
                TextField("id", in_form=False),
                TextField("name", required=True, searchable=True, inline_editable=True),
                CurrencyField("amount", inline_editable=True),
                StatusField("stage", choices=[("open", "Open"), ("won", "Won")],
                            inline_editable=True),
                TextField("owner"),
            ],
            views=[
                ListView(columns=[Column("name", link=True), "amount", "stage"]),
                FormView([Section("Deal", ["name", "amount", "stage", "owner"], columns=2)]),
            ],
        )
    )

    settings = Settings(
        secret_key="k" * 32, environment="test", template_reload=False,
        auth_providers=["session"], modules=[],
    )
    return TestClient(create_app(settings=settings, registry=registry),
                      raise_server_exceptions=False)


def grant(role: str, resource: str, *ops: str, rows: str = "all", **extra) -> dict:
    return {
        "role": role, "resource": resource,
        "can_read": "read" in ops, "can_create": "create" in ops,
        "can_update": "update" in ops, "can_delete": "delete" in ops,
        "row_scope": rows, **extra,
    }


@pytest.fixture(autouse=True)
def reset_store():
    yield
    store.provider = None
    store.invalidate()


def as_rep(client: TestClient) -> TestClient:
    sign_in(client, email="kim@x.test", roles=["user"])
    return client


def rows_in(html: str) -> int:
    return len(re.findall(r'data-pk="[^"]+"', html))


class TestGrantsGovernReads:
    def test_a_read_grant_shows_the_list(self):
        with build_app([grant("user", "deals", "read")]) as client:
            assert as_rep(client).get("/r/deals").status_code == 200

    def test_no_grant_refuses_the_list(self):
        with build_app([grant("manager", "deals", "read")]) as client:
            assert as_rep(client).get("/r/deals").status_code == 403

    def test_a_wildcard_grant_covers_the_resource(self):
        with build_app([grant("user", ANY_RESOURCE, "read")]) as client:
            assert as_rep(client).get("/r/deals").status_code == 200


class TestGrantsGovernRows:
    def test_scope_all_shows_everything(self):
        with build_app([grant("user", "deals", "read", rows="all")]) as client:
            payload = as_rep(client).get("/r/deals", headers={"Accept": "application/json"}).json()
            assert payload["total"] == 3

    def test_scope_own_shows_only_the_callers_rows(self):
        with build_app([grant("user", "deals", "read", rows="own")]) as client:
            payload = as_rep(client).get("/r/deals", headers={"Accept": "application/json"}).json()
            assert payload["total"] == 1
            assert payload["items"][0]["owner"] == "kim@x.test"

    def test_scope_own_hides_another_row_from_direct_access(self):
        with build_app([grant("user", "deals", "read", rows="own")]) as client:
            assert as_rep(client).get("/r/deals/2").status_code == 404

    def test_scope_own_applies_to_the_export(self):
        with build_app([grant("user", "deals", "read", rows="own")]) as client:
            body = as_rep(client).get("/r/deals/export.csv").text
            assert "Their deal" not in body


class TestGrantsGovernWrites:
    def test_without_update_an_edit_is_refused(self):
        with build_app([grant("user", "deals", "read")]) as client:
            rep = as_rep(client)
            assert rep.get("/r/deals/1/edit").status_code == 403

    def test_with_update_an_edit_is_allowed(self):
        with build_app([grant("user", "deals", "read", "update")]) as client:
            assert as_rep(client).get("/r/deals/1/edit").status_code == 200

    def test_without_delete_a_delete_is_refused(self):
        with build_app([grant("user", "deals", "read", "update")]) as client:
            rep = as_rep(client)
            token = csrf_from(rep, "/r/deals/1/edit")
            assert rep.post("/r/deals/1/delete", data={"csrf_token": token}).status_code == 403

    def test_with_delete_a_delete_is_allowed(self):
        with build_app([grant("user", "deals", "read", "update", "delete")]) as client:
            rep = as_rep(client)
            token = csrf_from(rep, "/r/deals/1/edit")
            response = rep.post("/r/deals/1/delete", data={"csrf_token": token},
                                follow_redirects=False)
            assert response.status_code == 303

    def test_without_create_the_new_form_is_refused(self):
        with build_app([grant("user", "deals", "read", "update")]) as client:
            assert as_rep(client).get("/r/deals/new").status_code == 403

    def test_own_scope_blocks_editing_someone_elses_row(self):
        with build_app([grant("user", "deals", "read", "update", rows="own")]) as client:
            rep = as_rep(client)
            token = csrf_from(rep, "/r/deals/1/edit")
            response = rep.post("/r/deals/2", data={"csrf_token": token, "name": "hijacked"})
            assert response.status_code == 404


class TestGrantsGovernButtons:
    """A control that would fail must not be drawn."""

    def test_no_create_grant_hides_the_new_button(self):
        with build_app([grant("user", "deals", "read")]) as client:
            assert "New deal" not in as_rep(client).get("/r/deals").text

    def test_a_create_grant_shows_it(self):
        with build_app([grant("user", "deals", "read", "create")]) as client:
            assert "New deal" in as_rep(client).get("/r/deals").text


class TestGrantsGovernFields:
    def test_a_hidden_field_is_absent_from_the_list(self):
        grants = [grant("user", "deals", "read", hidden_fields='["amount"]')]
        with build_app(grants) as client:
            html = as_rep(client).get("/r/deals").text
            assert "Own deal" in html
            assert "Amount" not in html

    def test_a_hidden_field_is_absent_from_the_export(self):
        grants = [grant("user", "deals", "read", hidden_fields='["amount"]')]
        with build_app(grants) as client:
            assert "Amount" not in as_rep(client).get("/r/deals/export.csv").text

    def test_a_readonly_field_cannot_be_written(self):
        grants = [grant("user", "deals", "read", "update", readonly_fields='["stage"]')]
        with build_app(grants) as client:
            rep = as_rep(client)
            token = csrf_from(rep, "/r/deals/1/edit")
            rep.post("/r/deals/1", data={"csrf_token": token, "name": "Own deal",
                                         "stage": "won"})
            record = rep.get("/r/deals/1", headers={"Accept": "application/json"}).json()
            assert record["stage"] == "open", "a locked field must not be writable"


class TestAdministratorsBypassTheTable:
    def test_an_admin_needs_no_grant(self):
        with build_app([]) as client:
            sign_in(client, email="admin@x.test", roles=["admin"])
            payload = client.get("/r/deals", headers={"Accept": "application/json"}).json()
            assert payload["total"] == 3


class TestChangingAGrantTakesEffect:
    def test_a_new_grant_applies_after_the_cache_is_dropped(self):
        with build_app([grant("user", "deals", "read", rows="own")]) as client:
            rep = as_rep(client)
            assert rep.get("/r/deals", headers={"Accept": "application/json"}).json()["total"] == 1

            # Widen the grant the way an administrator would: write through
            # the permissions resource itself, then drop the cached table.
            registry = client.app.state.crm.registry
            permissions = registry.resource("permissions").provider
            from app.core.results import Ctx

            await_sync(permissions.update(
                1, {"row_scope": "all"}, Ctx.system()
            ))
            store.invalidate()

            assert rep.get("/r/deals", headers={"Accept": "application/json"}).json()["total"] == 3
