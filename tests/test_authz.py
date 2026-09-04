"""Authorization.

Two questions, tested separately because they fail differently: may this person
perform this operation, and which rows may they see at all. The second is the
one that has to hold on every path, including the ones that never call `list`.
"""

from __future__ import annotations

import re

from .conftest import csrf_from, sign_in


def rows_in(html: str) -> int:
    return len(re.findall(r'data-pk="[^"]+"', html))


class TestRowScoping:
    """A rep sees their own records; a manager sees everything."""

    def test_a_rep_sees_only_their_own(self, rep):
        payload = rep.get("/r/deals", headers={"Accept": "application/json"}).json()
        assert payload["total"] == 2
        assert {r["owner"] for r in payload["items"]} == {"kim@example.com"}

    def test_a_manager_sees_everything(self, client):
        sign_in(client, email="boss@example.com", roles=["manager"])
        assert client.get("/r/deals", headers={"Accept": "application/json"}).json()["total"] == 3

    def test_the_total_reflects_the_scope_not_the_table(self, rep):
        # A total that counted every row would leak how much exists.
        assert rep.get("/r/deals", headers={"Accept": "application/json"}).json()["total"] == 2

    def test_a_scoped_out_record_reads_as_absent(self, rep):
        # 404 rather than 403: confirming the key exists is itself a leak.
        assert rep.get("/r/deals/2").status_code == 404

    def test_an_owned_record_is_readable(self, rep):
        assert rep.get("/r/deals/1").status_code == 200

    def test_scope_survives_an_or_filter(self, rep):
        # A user filter must never be able to widen the scope.
        response = rep.get(
            "/r/deals?f.owner=sam@example.com", headers={"Accept": "application/json"}
        )
        assert response.json()["total"] == 0

    def test_search_cannot_reach_outside_the_scope(self, rep):
        payload = rep.get(
            "/r/deals?q=Archive", headers={"Accept": "application/json"}
        ).json()
        assert payload["total"] == 0, "Archive migration belongs to sam"


class TestScopeOnEveryPath:
    """The restriction must hold on routes that never build a list query."""

    def test_edit_form(self, rep):
        assert rep.get("/r/deals/2/edit").status_code == 404

    def test_update(self, rep):
        token = csrf_from(rep, "/r/deals/1/edit")
        assert rep.post("/r/deals/2", data={"csrf_token": token, "name": "x"}).status_code == 404

    def test_delete(self, rep):
        token = csrf_from(rep, "/r/deals/1/edit")
        assert rep.post("/r/deals/2/delete", data={"csrf_token": token}).status_code == 404

    def test_inline_field_edit(self, rep):
        token = csrf_from(rep, "/r/deals/1/edit")
        response = rep.post(
            "/r/deals/2/field/name", data={"csrf_token": token, "name": "x"}
        )
        assert response.status_code == 404

    def test_csv_export(self, rep):
        body = rep.get("/r/deals/export.csv").text
        assert "Archive migration" not in body
        assert len([line for line in body.splitlines() if line.strip()]) == 3

    def test_the_chart_aggregate(self, rep):
        # A different provider method entirely, and it must still be scoped.
        rows = rep.get("/r/deals?view=chart", headers={"Accept": "application/json"}).json()["rows"]
        total = sum(r["value"] or 0 for r in rows)
        assert total == 2, "only kim's two deals"

    def test_the_board(self, rep):
        html = rep.get("/r/deals?view=board").text
        assert "Archive migration" not in html

    def test_the_dashboard_count(self, rep):
        html = rep.get("/").text
        # The card shows 2, not 3.
        assert re.search(r'stat-value">2<', html)

    def test_bulk_delete_cannot_reach_other_rows(self, rep):
        token = csrf_from(rep, "/r/deals/1/edit")
        rep.post("/r/deals/bulk/delete", data={"csrf_token": token, "selected": ["2"]})
        # Signed in as someone who can see it, confirm it survived.
        sign_in(rep, email="boss@example.com", roles=["manager"])
        assert rep.get("/r/deals/2").status_code == 200


class TestRolePolicy:
    def test_a_restricted_resource_is_refused(self, rep):
        assert rep.get("/r/users").status_code == 403

    def test_an_admin_may_read_it(self, admin):
        assert admin.get("/r/users").status_code == 200

    def test_a_refused_resource_is_absent_from_the_menu(self, rep):
        # Showing it disabled would confirm it exists.
        assert 'href="/r/users"' not in rep.get("/r/contacts").text

    def test_it_is_present_for_an_admin(self, admin):
        assert 'href="/r/users"' in admin.get("/r/contacts").text


class TestFieldLevelAccess:
    def test_a_hidden_field_never_reaches_the_page(self, admin):
        assert "password_hash" not in admin.get("/r/users").text

    def test_a_hidden_field_never_reaches_an_export(self, admin):
        assert "password_hash" not in admin.get("/r/users/export.csv").text.lower()

    def test_a_hidden_field_cannot_be_written_through_a_form(self, admin):
        token = csrf_from(admin, "/r/users/new")
        admin.post(
            "/r/users",
            data={
                "csrf_token": token, "name": "New", "email": "new@example.com",
                "password_hash": "injected",
            },
        )
        payload = admin.get("/r/users", headers={"Accept": "application/json"}).json()
        created = next(r for r in payload["items"] if r["email"] == "new@example.com")
        assert created.get("password_hash") in (None, ""), "a field the form never offered must not be writable"


class TestCapabilityAwarePermissions:
    """A backend that cannot write must not be offered write controls."""

    def test_a_read_only_provider_hides_the_create_button(self, client, app):
        from app.providers.composite import ReadOnly

        registry = app.state.crm.registry
        contacts = registry.resource("contacts")
        contacts.provider = ReadOnly(contacts.provider)
        sign_in(client, email="admin@example.com", roles=["admin"])
        assert "New contact" not in client.get("/r/contacts").text

    def test_the_policy_alone_does_not_grant_what_the_backend_lacks(self, client, app):
        from app.providers.composite import ReadOnly

        registry = app.state.crm.registry
        contacts = registry.resource("contacts")
        contacts.provider = ReadOnly(contacts.provider)
        sign_in(client, email="admin@example.com", roles=["admin"])
        assert contacts.policy.allows("create", app.state.crm.registry and _identity()) is True
        assert client.get("/r/contacts/new").status_code == 403


def _identity():
    from app.core.results import Identity

    return Identity(subject="admin@example.com", roles=frozenset({"admin"}))
