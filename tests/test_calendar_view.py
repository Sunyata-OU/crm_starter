"""A calendar names its events through the field, like every other view.

The failure this guards against is quiet: a computed title has nothing stored
under its key, so a raw ``record.get`` returns None and the grid places every
event correctly with the label "None". Nothing errors, so only a test that
looks at what the chip actually says will catch it.
"""

from __future__ import annotations

from datetime import date

import pytest
from starlette.testclient import TestClient

from app.fields.types import ComputedField, DateField, TextField
from app.main import create_app
from app.providers.memory import MemoryProvider
from app.resources.resource import Resource
from app.resources.views import CalendarView
from tests.conftest import build_registry, sign_in

SHIFT_ROWS = [
    {"id": 1, "who": "Ada", "role": "lead", "start": date(2026, 9, 3)},
    {"id": 2, "who": "Grace", "role": "relief", "start": date(2026, 9, 17)},
    # Outside the month under test, so the query's own bounds are exercised.
    {"id": 3, "who": "Alan", "role": "lead", "start": date(2026, 10, 2)},
]


def shifts_resource() -> Resource:
    return Resource(
        "shifts",
        provider=MemoryProvider(SHIFT_ROWS),
        display_field="label",
        fields=[
            TextField("id", in_form=False),
            TextField("who"),
            TextField("role"),
            DateField("start"),
            ComputedField("label", compute=lambda r: f"{r['who']} ({r['role']})"),
        ],
        views=[CalendarView(start_field="start", title_field="label")],
    )


@pytest.fixture
def shifts_client(settings):
    registry = build_registry()
    registry.add_resource(shifts_resource())
    with TestClient(create_app(settings=settings, registry=registry)) as c:
        sign_in(c, email="admin@example.com", roles=["admin"])
        yield c


class TestTheCalendarTitle:
    def test_a_computed_title_renders_its_computed_value(self, shifts_client):
        html = shifts_client.get("/r/shifts?view=calendar&year=2026&month=9").text
        assert "Ada (lead)" in html
        assert "Grace (relief)" in html

    def test_no_event_is_labelled_none(self, shifts_client):
        # The symptom of reading the record instead of the field.
        html = shifts_client.get("/r/shifts?view=calendar&year=2026&month=9").text
        assert ">\n                  None\n" not in html
        assert 'title="None"' not in html

    def test_a_record_outside_the_month_is_not_placed(self, shifts_client):
        html = shifts_client.get("/r/shifts?view=calendar&year=2026&month=9").text
        assert "Alan (lead)" not in html


class TestDisplayValue:
    """The same read, on the path that names a record in a link or typeahead."""

    def test_a_computed_display_field_names_the_record(self):
        resource = shifts_resource()
        assert resource.display_value({"id": 1, "who": "Ada", "role": "lead"}) == "Ada (lead)"

    def test_a_stored_display_field_still_works(self):
        resource = shifts_resource()
        resource.display_field = "who"
        assert resource.display_value({"id": 1, "who": "Ada"}) == "Ada"

    def test_a_record_with_no_display_value_falls_back_to_its_key(self):
        resource = shifts_resource()
        resource.display_field = "who"
        assert resource.display_value({"id": 7, "who": ""}).endswith("#7")

    def test_a_display_field_that_is_not_declared_reads_the_mapping(self):
        # A provider alias -- an aggregate, say -- is not a declared field.
        resource = shifts_resource()
        resource.display_field = "alias"
        assert resource.display_value({"id": 1, "alias": "from the query"}) == "from the query"


class TestSearchWithAComputedDisplayField:
    """Search falls back to the display field. A computed one is not a column."""

    def test_a_computed_display_field_is_not_offered_to_the_provider(self):
        assert shifts_resource().searchable_fields() == ()

    def test_a_stored_display_field_is_still_the_fallback(self):
        resource = shifts_resource()
        resource.display_field = "who"
        assert resource.searchable_fields() == ("who",)

    def test_an_undeclared_display_field_is_left_to_the_provider(self):
        resource = shifts_resource()
        resource.display_field = "alias"
        assert resource.searchable_fields() == ("alias",)

    def test_searching_a_calendar_with_a_computed_title_does_not_raise(self, shifts_client):
        response = shifts_client.get("/r/shifts?view=calendar&year=2026&month=9&q=Ada")
        assert response.status_code == 200
