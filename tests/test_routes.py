"""The generic resource routes.

Every resource is served by one set of handlers, so these tests are really
testing the machinery rather than the demo data: what a list renders, how a
form rejects bad input, whether a scoped user can reach another's record.
"""

from __future__ import annotations

import re

import pytest

from .conftest import csrf_from, sign_in


def rows_in(html: str) -> int:
    return len(re.findall(r'data-pk="[^"]+"', html))


class TestAuthenticationRequired:
    def test_anonymous_is_sent_to_sign_in(self, client):
        response = client.get("/r/contacts", follow_redirects=False)
        assert response.status_code == 303
        assert "/login" in response.headers["location"]

    def test_the_destination_is_preserved(self, client):
        response = client.get("/r/contacts?q=ada", follow_redirects=False)
        assert "next=/r/contacts" in response.headers["location"]

    def test_an_api_caller_gets_401_not_a_redirect(self, client):
        response = client.get("/r/contacts", headers={"Accept": "application/json"})
        assert response.status_code == 401

    def test_an_htmx_request_is_told_to_navigate(self, client):
        # A fragment request cannot usefully follow a redirect.
        response = client.get("/r/contacts", headers={"HX-Request": "true"})
        assert response.status_code == 204
        assert "/login" in response.headers["HX-Redirect"]


class TestListView:
    def test_renders_every_row(self, admin):
        assert rows_in(admin.get("/r/contacts").text) == 3

    def test_shows_the_declared_columns(self, admin):
        html = admin.get("/r/contacts").text
        assert "Ada Lovelace" in html and "ada@analytical.test" in html

    def test_search_narrows_the_list(self, admin):
        assert rows_in(admin.get("/r/contacts?q=hopper").text) == 1

    def test_search_looks_in_every_configured_field(self, admin):
        assert rows_in(admin.get("/r/contacts?q=bletchley").text) == 1

    def test_filters_narrow_the_list(self, admin):
        assert rows_in(admin.get("/r/contacts?f.status=active").text) == 2

    def test_operator_filters_work(self, admin):
        assert rows_in(admin.get("/r/contacts?f.name__icontains=lov").text) == 1

    def test_a_filter_on_an_unknown_field_is_ignored(self, admin):
        # A stale or hand-edited URL must not error, and must not widen access.
        assert rows_in(admin.get("/r/contacts?f.nonexistent=x").text) == 3

    def test_an_unparseable_filter_value_is_ignored(self, admin):
        assert admin.get("/r/deals?f.amount__gte=notanumber").status_code == 200

    def test_sorting_reorders(self, admin):
        ascending = admin.get("/r/deals?sort=amount").text
        descending = admin.get("/r/deals?sort=-amount").text
        assert re.findall(r'data-pk="([^"]+)"', ascending) == list(
            reversed(re.findall(r'data-pk="([^"]+)"', descending))
        )

    def test_sorting_by_an_unknown_column_is_ignored(self, admin):
        assert admin.get("/r/deals?sort=nonexistent").status_code == 200

    def test_pagination_slices(self, admin):
        assert rows_in(admin.get("/r/contacts?page_size=2").text) == 2
        assert rows_in(admin.get("/r/contacts?page_size=2&page=2").text) == 1

    def test_an_absurd_page_size_is_clamped(self, admin):
        # Untrusted input, bounded at the boundary rather than in the model.
        assert admin.get("/r/contacts?page_size=99999999").status_code == 200

    def test_htmx_gets_the_table_only(self, admin):
        fragment = admin.get("/r/contacts", headers={"HX-Request": "true"}).text
        assert "<table" in fragment
        assert "<nav class=\"sidebar\"" not in fragment, "a fragment must not carry the chrome"

    def test_json_is_served_when_asked_for(self, admin):
        payload = admin.get("/r/contacts", headers={"Accept": "application/json"}).json()
        assert payload["total"] == 3
        assert payload["items"][0]["name"]

    def test_an_unknown_resource_is_a_404(self, admin):
        assert admin.get("/r/nonexistent").status_code == 404


class TestRelationRendering:
    def test_relations_show_the_target_label_not_the_key(self, admin):
        html = admin.get("/r/contacts").text
        assert "Analytical Engines" in html

    def test_a_null_relation_renders_blank_rather_than_breaking(self, admin):
        # Grace Hopper has no company.
        assert admin.get("/r/contacts").status_code == 200

    def test_the_label_is_not_taken_from_the_wrong_record(self, admin):
        # The target's display field is "name", which contacts also have. The
        # company link must never show the contact's own name.
        html = admin.get("/r/contacts").text
        assert 'class="link relation">Ada Lovelace' not in html


class TestDetailView:
    def test_renders_the_record(self, admin):
        html = admin.get("/r/contacts/1").text
        assert "Ada Lovelace" in html

    def test_missing_record_is_a_404(self, admin):
        assert admin.get("/r/contacts/999").status_code == 404

    def test_json_returns_the_record(self, admin):
        assert admin.get("/r/contacts/1", headers={"Accept": "application/json"}).json()["name"] == "Ada Lovelace"


class TestCreate:
    def test_the_form_renders(self, admin):
        assert "csrf_token" in admin.get("/r/contacts/new").text

    def test_a_valid_submission_creates_and_redirects(self, admin):
        token = csrf_from(admin, "/r/contacts/new")
        response = admin.post(
            "/r/contacts",
            data={"csrf_token": token, "name": "Sophie Wilson", "email": "sophie@test.dev"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert re.match(r"/r/contacts/\d+", response.headers["location"])

    def test_the_new_record_appears(self, admin):
        token = csrf_from(admin, "/r/contacts/new")
        admin.post("/r/contacts", data={"csrf_token": token, "name": "Sophie", "email": "s@t.dev"})
        assert rows_in(admin.get("/r/contacts").text) == 4

    def test_a_missing_required_field_is_rejected(self, admin):
        token = csrf_from(admin, "/r/contacts/new")
        response = admin.post("/r/contacts", data={"csrf_token": token, "email": "a@b.dev"})
        assert response.status_code == 422
        assert "Name is required" in response.text

    def test_an_invalid_value_is_rejected_with_a_field_message(self, admin):
        token = csrf_from(admin, "/r/contacts/new")
        response = admin.post(
            "/r/contacts", data={"csrf_token": token, "name": "X", "email": "not-an-email"}
        )
        assert response.status_code == 422
        assert "valid email" in response.text

    def test_a_rejected_form_keeps_what_was_typed(self, admin):
        token = csrf_from(admin, "/r/contacts/new")
        response = admin.post(
            "/r/contacts", data={"csrf_token": token, "name": "X", "email": "not-an-email"}
        )
        assert 'value="not-an-email"' in response.text

    def test_initial_values_can_be_seeded_from_the_url(self, admin):
        # Used by "add a contact" from a company page.
        assert 'value="2"' in admin.get("/r/contacts/new?with.company_id=2").text


class TestUpdate:
    def test_the_edit_form_is_populated(self, admin):
        assert 'value="Ada Lovelace"' in admin.get("/r/contacts/1/edit").text

    def test_a_valid_edit_is_applied(self, admin):
        token = csrf_from(admin, "/r/contacts/1/edit")
        admin.post(
            "/r/contacts/1",
            data={"csrf_token": token, "name": "Ada King", "email": "ada@analytical.test"},
        )
        assert "Ada King" in admin.get("/r/contacts/1").text

    def test_an_invalid_edit_is_rejected(self, admin):
        token = csrf_from(admin, "/r/contacts/1/edit")
        response = admin.post(
            "/r/contacts/1", data={"csrf_token": token, "name": "A", "email": "bad"}
        )
        assert response.status_code == 422


class TestDelete:
    def test_removes_the_record(self, admin):
        token = csrf_from(admin, "/r/contacts/1/edit")
        admin.post("/r/contacts/1/delete", data={"csrf_token": token})
        assert admin.get("/r/contacts/1").status_code == 404

    def test_bulk_delete_removes_the_selection(self, admin):
        token = csrf_from(admin, "/r/contacts/new")
        admin.post("/r/contacts/bulk/delete", data={"csrf_token": token, "selected": ["1", "2"]})
        assert rows_in(admin.get("/r/contacts").text) == 1

    def test_bulk_delete_with_nothing_selected_is_harmless(self, admin):
        token = csrf_from(admin, "/r/contacts/new")
        response = admin.post("/r/contacts/bulk/delete", data={"csrf_token": token})
        assert response.status_code in (200, 303)
        assert rows_in(admin.get("/r/contacts").text) == 3


class TestInlineEditing:
    def test_the_editor_fragment_is_returned(self, admin):
        html = admin.get("/r/contacts/1/field/name", headers={"HX-Request": "true"}).text
        assert "cell-editing" in html and 'value="Ada Lovelace"' in html

    def test_saving_returns_the_display_cell(self, admin):
        token = csrf_from(admin, "/r/contacts/1/edit")
        response = admin.post(
            "/r/contacts/1/field/name",
            data={"csrf_token": token, "name": "Ada King"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert "Ada King" in response.text
        assert "cell-editing" not in response.text

    def test_saving_one_field_leaves_the_others_alone(self, admin):
        token = csrf_from(admin, "/r/contacts/1/edit")
        admin.post(
            "/r/contacts/1/field/name",
            data={"csrf_token": token, "name": "Ada King"},
            headers={"HX-Request": "true"},
        )
        record = admin.get("/r/contacts/1", headers={"Accept": "application/json"}).json()
        assert record["email"] == "ada@analytical.test", "a partial write must not blank other columns"

    def test_an_invalid_value_keeps_the_editor_open(self, admin):
        token = csrf_from(admin, "/r/contacts/1/edit")
        response = admin.post(
            "/r/contacts/1/field/email",
            data={"csrf_token": token, "email": "nope"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 422
        assert "cell-editing" in response.text

    def test_a_field_not_marked_inline_editable_is_not_editable(self, admin):
        token = csrf_from(admin, "/r/contacts/1/edit")
        response = admin.post(
            "/r/contacts/1/field/notes", data={"csrf_token": token, "notes": "x"}
        )
        assert response.status_code == 422


class TestCSRF:
    def test_a_write_without_a_token_is_refused(self, admin):
        assert admin.post("/r/contacts", data={"name": "X", "email": "a@b.dev"}).status_code == 403

    def test_a_forged_token_is_refused(self, admin):
        response = admin.post(
            "/r/contacts", data={"csrf_token": "forged", "name": "X", "email": "a@b.dev"}
        )
        assert response.status_code == 403

    def test_another_users_token_is_refused(self, client):
        # Tokens are bound to the session that issued them.
        sign_in(client, email="kim@example.com", roles=["admin"])
        token = csrf_from(client, "/r/contacts/new")
        sign_in(client, email="other@example.com", roles=["admin"])
        response = client.post(
            "/r/contacts", data={"csrf_token": token, "name": "X", "email": "a@b.dev"}
        )
        assert response.status_code == 403

    def test_reads_need_no_token(self, admin):
        assert admin.get("/r/contacts").status_code == 200

    def test_every_state_changing_route_is_covered(self, admin):
        """The check is a dependency, not a line each handler remembers.

        Written after finding that POST /logout had been added without one.
        Enumerating the routes means a new handler is covered by construction,
        and this test fails if the protection is ever moved back into the
        handlers and one of them is missed.
        """
        unsafe = [
            "/logout",
            "/account/password",
            "/r/contacts",
            "/r/contacts/1",
            "/r/contacts/1/delete",
            "/r/contacts/1/field/name",
            "/r/deals/1/action/mark_won",
            "/r/contacts/bulk/delete",
            "/notifications/read-all",
        ]
        for path in unsafe:
            assert admin.post(path, data={"name": "X"}).status_code == 403, path


class TestAlternativeViews:
    def test_the_board_groups_by_its_field(self, admin):
        html = admin.get("/r/deals?view=board").text
        assert "Open" in html and "Won" in html and "board-card" in html

    def test_the_board_totals_each_column(self, admin):
        assert "board-sum" in admin.get("/r/deals?view=board").text

    def test_the_chart_renders_bars(self, admin):
        html = admin.get("/r/deals?view=chart").text
        assert "chart-bar" in html

    def test_the_chart_serves_json(self, admin):
        rows = admin.get("/r/deals?view=chart", headers={"Accept": "application/json"}).json()["rows"]
        assert {r["stage"] for r in rows} == {"open", "won"}

    def test_an_unknown_view_falls_back_to_the_list(self, admin):
        assert admin.get("/r/deals?view=nonsense").status_code == 200


class TestExport:
    def test_csv_has_a_header_and_a_row_per_record(self, admin):
        body = admin.get("/r/contacts/export.csv").text
        lines = [line for line in body.splitlines() if line.strip()]
        assert lines[0].startswith("Name,")
        assert len(lines) == 4

    def test_csv_respects_the_current_filter(self, admin):
        body = admin.get("/r/contacts/export.csv?f.status=active").text
        assert len([line for line in body.splitlines() if line.strip()]) == 3

    def test_csv_is_offered_as_a_download(self, admin):
        response = admin.get("/r/contacts/export.csv")
        assert "attachment" in response.headers["content-disposition"]


class TestTypeahead:
    def test_returns_matching_options(self, admin):
        payload = admin.get(
            "/r/companies/options?q=analytical", headers={"Accept": "application/json"}
        ).json()
        assert payload["options"][0]["label"] == "Analytical Engines"

    def test_the_route_is_not_read_as_a_record_key(self, admin):
        # /r/companies/options must not match /r/companies/{pk}.
        assert "options" in admin.get(
            "/r/companies/options", headers={"Accept": "application/json"}
        ).json()


class TestSystemPages:
    def test_the_dashboard_counts_each_resource(self, admin):
        html = admin.get("/").text
        assert "Dashboard" in html and "Contacts" in html

    def test_health_needs_no_authentication(self, client):
        assert client.get("/healthz").json() == {"status": "ok"}

    def test_whoami_reports_the_identity(self, admin):
        payload = admin.get("/whoami").json()
        assert payload["authenticated"] and payload["email"] == "admin@example.com"

    def test_the_system_page_is_admin_only(self, rep):
        assert rep.get("/system").status_code == 403

    def test_the_system_page_shows_backend_capabilities(self, admin):
        html = admin.get("/system").text
        assert "native" in html or "emulated" in html


class TestTimeline:
    """A record's history, read from the audit log."""

    def test_the_route_exists(self, admin):
        # It did not, for a while: the detail template asked for a URL that
        # returned 404 on every page it appeared on.
        assert admin.get("/r/contacts/1/timeline").status_code == 200

    def test_an_empty_history_says_so(self, admin):
        assert "No history yet" in admin.get("/r/contacts/1/timeline").text

    def test_history_is_refused_for_a_record_the_caller_cannot_see(self, rep):
        # Otherwise the history leaks what the record itself does not.
        assert rep.get("/r/deals/2/timeline").status_code == 404


class TestInlineEditCancel:
    def test_cancelling_returns_the_display_cell(self, admin):
        # Without this the cancel button hands back another editor, which looks
        # like a button that does nothing.
        html = admin.get(
            "/r/contacts/1/field/name?cancel=1", headers={"HX-Request": "true"}
        ).text
        assert "cell-editing" not in html
        assert "Ada Lovelace" in html


class TestViewAliases:
    def test_a_board_answers_to_kanban(self, admin):
        board = admin.get("/r/deals?view=board").text
        kanban = admin.get("/r/deals?view=kanban").text
        assert "board-card" in kanban
        assert board.count("board-card") == kanban.count("board-card")

    def test_an_unknown_view_still_falls_back(self, admin):
        assert admin.get("/r/deals?view=nonsense").status_code == 200


class TestRelationLabelBatching:
    """Relation labels are fetched in batches, not capped.

    The bug this guards is quiet: past the batch size the excess rows rendered
    their raw key instead of a name, which reads as missing data rather than as
    a limit. It also keeps the IN clause under SQLite's parameter ceiling.
    """

    def test_more_keys_than_one_batch_are_all_resolved(self, monkeypatch):
        import asyncio

        from app.web.routes import resource as routes

        monkeypatch.setattr(routes, "LABEL_BATCH", 2)

        seen: list[int] = []

        class Target:
            name = "companies"
            pk = "id"

            def build_query(self, identity, *, filter, page_size, with_total):
                seen.append(page_size)
                self.last = filter
                return filter

            @staticmethod
            def display_value(record):
                return f"company {record.pk}"

            class provider:
                @staticmethod
                async def list(query, ctx):
                    from app.core.query import Page
                    from app.core.results import Record

                    return Page(items=[Record({"id": k}, "id") for k in query.value])

        class View:
            relation_labels: dict = {}
            identity = None
            ctx = None

        labels = asyncio.run(
            routes._relation_labels(View(), Target(), {"1", "2", "3", "4", "5"})
        )

        assert seen == [2, 2, 1], "keys must be fetched in batches, in full"
        assert set(labels) == {"1", "2", "3", "4", "5"}


class TestNarrowScreenMarkup:
    """The list stops being a table on a phone, and that needs two things.

    A stylesheet cannot invent a column header to put beside a value, so the
    markup carries one per cell. And it must not do this to a pivot, which is a
    matrix that stays a matrix. Both are markup contracts between the template
    and the stylesheet, so both are asserted here -- a CSS-only change that
    silently stopped matching would otherwise be invisible until someone opened
    a phone.
    """

    def test_every_list_cell_carries_its_own_label(self, client, admin):
        html = client.get("/r/contacts").text
        assert 'class="data-table stackable"' in html
        assert html.count("data-label=") >= 3
        for label in ("Name", "Email"):
            assert f'data-label="{label}"' in html

    def test_a_pivot_is_not_stacked(self):
        """A matrix with its rows stacked is a pile of numbers.

        Read from the template rather than a rendered page: the fixture
        registry has no pivot, and what is being asserted is the template's
        choice not to opt in, not any particular resource's views.
        """
        from app.settings import APP_DIR

        pivot = (APP_DIR / "templates" / "views" / "pivot.html").read_text()
        assert "data-table pivot" in pivot
        assert "stackable" not in pivot

    def test_a_calendar_scrolls_rather_than_reflowing(self):
        from app.settings import APP_DIR

        calendar = (APP_DIR / "templates" / "views" / "calendar.html").read_text()
        assert "matrix-wrap" in calendar


class TestResponsiveStylesheet:
    """The rules the markup above exists for.

    Asserted against the file rather than a browser: a rule that goes missing
    is a layout that silently reverts to a sideways-scrolling table, which is
    exactly what the markup was added to prevent.
    """

    @pytest.fixture
    def css(self) -> str:
        from app.settings import APP_DIR

        return (APP_DIR / "static" / "crm.css").read_text()

    def test_there_is_a_phone_breakpoint(self, css):
        assert "@media (max-width: 640px)" in css

    def test_cells_are_labelled_from_the_markup(self, css):
        assert "content: attr(data-label)" in css

    def test_only_stackable_tables_are_stacked(self, css):
        block = css.split("@media (max-width: 640px)")[1]
        stacking = [
            line.strip() for line in block.splitlines()
            if "display: block" in line and ("table" in line or "tbody" in line or "tr," in line)
        ]
        assert stacking, "nothing turns a table into blocks"
        assert all("stackable" in line for line in stacking), (
            f"a rule stacks tables generally, which would break the pivot: {stacking}"
        )

    def test_inputs_do_not_trigger_ios_zoom(self, css):
        """Under 16px, iOS zooms on focus and leaves the page scrolled sideways."""
        block = css.split("@media (max-width: 640px)")[1]
        assert "font-size: 16px" in block

    def test_the_sidebar_becomes_a_drawer(self, css):
        assert "body.nav-open .sidebar" in css
