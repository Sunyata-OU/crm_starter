"""Entering several records at once, and reporting what became of them.

`RowsField` is a form field whose value is a table of forms. It exists for the
action whose subject is a *set* of new things, where the alternative is asking
somebody to write a CSV and find out afterwards which line was wrong. Here the
grid is what is submitted, every cell validated by the field that owns the
column, so a bad value is a message beside the row it came from.

What comes back is the other half: forty attempts against something remote
produce forty answers, and `ActionResult.template` gives them somewhere to be
read that is not a toast fading after four seconds.
"""

from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from app.core.results import Ctx, Identity
from app.fields.types import EmailField, RowsField, SelectField, TextField
from app.main import create_app
from app.providers.memory import MemoryProvider
from app.resources.actions import ActionResult, RowOutcome, action
from app.settings import Settings
from tests.conftest import sign_in

ADMIN = Identity(subject="t", roles=("admin",), claims={})
CTX = Ctx(identity=ADMIN, request_id="r")

TIERS = [("std", "Standard"), ("vip", "VIP")]

GRID = RowsField(
    "people",
    label="People",
    columns=[
        TextField("name", label="Name", required=True),
        EmailField("email", label="Email", required=True),
        SelectField("tier", label="Tier", choices=TIERS),
    ],
    min_rows=3,
)

#: What the handler was handed, for the assertions.
CALLS: list[list[dict]] = []


@action("invite", "Invite people", placements=("bulk",), prompt_fields=[GRID])
async def invite(records, ctx, resource, *, params) -> ActionResult:
    rows = params["people"]
    CALLS.append(rows)
    outcomes = [
        RowOutcome(row["email"], "@example.org" not in row["email"], "domain not invited")
        for row in rows
    ]
    result = ActionResult.from_outcomes(outcomes, done="Invited")
    result.template = "views/_action_report.html"
    result.data = {
        "headings": ["Email", "Result"],
        "rows": [[o.pk, o.message or "invited"] for o in outcomes],
    }
    return result


def build_registry():
    from app.core.registry import Registry
    from app.resources.resource import Resource
    from app.resources.views import Column, ListView

    registry = Registry()
    registry.add_resource(
        Resource(
            "guests",
            provider=MemoryProvider([{"id": "1", "name": "Ada"}]),
            fields=[TextField("id", in_form=False), TextField("name")],
            actions=[invite],
            views=[ListView(columns=[Column("name", link=True)], bulk_actions=["invite"])],
        )
    )
    return registry


@pytest.fixture
def client():
    settings = Settings(
        environment="test",
        secret_key="test-key-not-for-real-use",
        debug=False,
        template_reload=False,
        csrf_enabled=True,
        auth_providers=["session", "local"],
        modules=[],
    )
    app = create_app(settings=settings, registry=build_registry())
    CALLS.clear()
    with TestClient(app, raise_server_exceptions=False) as c:
        sign_in(c, email="admin@example.com", roles=["admin"])
        yield c


def token_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no CSRF token in the dialog"
    return match.group(1)


def dialog(client) -> str:
    response = client.get("/r/guests/bulk/invite/prompt?selected=1")
    assert response.status_code == 200
    return response.text


def submit(client, html: str, rows: list[tuple[str, str, str]]):
    """Post the grid the way a browser does: one repeated key per column."""
    columns = list(zip(*rows, strict=True)) if rows else [[], [], []]
    return client.post(
        "/r/guests/bulk/invite",
        data={
            "csrf_token": token_from(html),
            "selected": "1",
            "people.name": list(columns[0]),
            "people.email": list(columns[1]),
            "people.tier": list(columns[2]),
        },
    )


class TestTheDialogIsAGrid:
    def test_every_column_gets_its_own_input(self, client):
        html = dialog(client)
        assert 'name="people.name"' in html
        assert 'name="people.email"' in html
        # A select stays a select: the grid is the form, not a spreadsheet.
        assert 'name="people.tier"' in html and "<option" in html

    def test_it_opens_with_blank_rows_to_type_into(self, client):
        assert dialog(client).count('name="people.email"') == 3

    def test_the_file_picker_is_never_submitted(self, client):
        html = dialog(client)
        assert "crm.gridFromCsv" in html
        assert 'type="file" accept=".csv,text/csv" hidden' in html


class TestWhatTheHandlerIsGiven:
    def test_the_rows_arrive_as_records(self, client):
        submit(client, dialog(client), [
            ("Ada", "ada@example.org", "vip"),
            ("Grace", "grace@example.org", "std"),
        ])
        assert CALLS == [[
            {"name": "Ada", "email": "ada@example.org", "tier": "vip"},
            {"name": "Grace", "email": "grace@example.org", "tier": "std"},
        ]]

    def test_a_row_nobody_filled_in_is_dropped_not_refused(self, client):
        """Three blank rows is how the dialog opens; entering two records in it
        is the usual case, not a mistake to report."""
        submit(client, dialog(client), [
            ("Ada", "ada@example.org", "vip"),
            ("", "", ""),
        ])
        assert [row["name"] for row in CALLS[0]] == ["Ada"]

    def test_a_bad_cell_names_its_row_and_column(self, client):
        response = submit(client, dialog(client), [
            ("Ada", "ada@example.org", "vip"),
            ("Grace", "not-an-email", "std"),
        ])
        assert CALLS == [], "nothing ran"
        assert "Row 2, Email" in response.text

    def test_a_missing_required_cell_is_the_same_kind_of_message(self, client):
        response = submit(client, dialog(client), [("", "ada@example.org", "vip")])
        assert CALLS == []
        assert "Row 1, Name" in response.text

    def test_the_rejected_grid_comes_back_with_the_values_in_it(self, client):
        """Retyping eleven good rows because the twelfth was wrong is the
        failure this field exists to avoid."""
        response = submit(client, dialog(client), [
            ("Ada", "ada@example.org", "vip"),
            ("Grace", "not-an-email", "std"),
        ])
        assert 'value="ada@example.org"' in response.text
        assert 'value="not-an-email"' in response.text


class TestTheReportStaysOnScreen:
    def test_each_row_is_reported_by_name(self, client):
        response = submit(client, dialog(client), [
            ("Ada", "ada@example.org", "vip"),
            ("Bob", "bob@elsewhere.test", "std"),
        ])
        assert "ada@example.org" in response.text
        assert "domain not invited" in response.text

    def test_a_partial_failure_says_so_rather_than_claiming_success(self, client):
        response = submit(client, dialog(client), [
            ("Ada", "ada@example.org", "vip"),
            ("Bob", "bob@elsewhere.test", "std"),
        ])
        assert re.search(r"1 of 2|1 failed|Invited 1", response.text)

    def test_it_is_a_dialog_and_not_a_toast(self, client):
        response = submit(client, dialog(client), [("Ada", "ada@example.org", "vip")])
        assert 'role="dialog"' in response.text


class TestTheFieldOnItsOwn:
    """Collected without a request, because the rules belong to the field."""

    def test_nothing_submitted_is_empty_rather_than_an_empty_list(self):
        from app.fields.base import EMPTY

        assert GRID.collect({}, CTX) is EMPTY

    def test_the_columns_are_read_in_parallel(self):
        from starlette.datastructures import FormData

        data = FormData([
            ("people.name", "Ada"), ("people.email", "ada@example.org"),
            ("people.name", "Grace"), ("people.email", "grace@example.org"),
        ])
        assert GRID.collect(data, CTX) == [
            {"name": "Ada", "email": "ada@example.org", "tier": None},
            {"name": "Grace", "email": "grace@example.org", "tier": None},
        ]

    def test_it_is_not_a_column_and_refuses_to_be_stored(self):
        from app.core.errors import UnsupportedOperation

        with pytest.raises(UnsupportedOperation):
            GRID.to_storage([{"name": "Ada"}])

    def test_it_stays_out_of_lists_and_filters(self):
        assert GRID.in_list is False and GRID.in_filter is False
