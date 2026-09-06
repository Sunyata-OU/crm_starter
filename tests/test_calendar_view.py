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


class TestTheCalendarScale:
    """A day, a week and a fortnight are the month grid seen through a
    narrower window. What is worth testing is the window, not the table: which
    records the query reaches for, and where "previous" lands."""

    def test_a_week_shows_only_its_own_week(self, shifts_client):
        # 2026-09-03 is a Thursday, so its week is the 31st to the 6th.
        html = shifts_client.get("/r/shifts?view=calendar&scale=week&at=2026-09-03").text
        assert "Ada (lead)" in html
        assert "Grace (relief)" not in html

    def test_a_week_begins_on_monday_whichever_day_is_asked_for(self, shifts_client):
        for day in ("2026-08-31", "2026-09-03", "2026-09-06"):
            html = shifts_client.get(f"/r/shifts?view=calendar&scale=week&at={day}").text
            assert "31 Aug – 6 Sep 2026" in html

    def test_a_fortnight_reaches_a_day_the_week_does_not(self, shifts_client):
        # The 17th is in the second week of the fortnight beginning the 7th.
        week = shifts_client.get("/r/shifts?view=calendar&scale=week&at=2026-09-07").text
        fortnight = shifts_client.get(
            "/r/shifts?view=calendar&scale=fortnight&at=2026-09-07"
        ).text
        assert "Grace (relief)" not in week
        assert "Grace (relief)" in fortnight

    def test_a_day_shows_that_day_alone(self, shifts_client):
        html = shifts_client.get("/r/shifts?view=calendar&scale=day&at=2026-09-03").text
        assert "Ada (lead)" in html
        assert "Grace (relief)" not in html
        # One cell, so one column header -- the weekday the day actually is.
        assert html.count("<th>") == 1
        assert "<th>Thu</th>" in html

    def test_previous_steps_back_one_whole_window(self, shifts_client):
        html = shifts_client.get("/r/shifts?view=calendar&scale=fortnight&at=2026-09-14").text
        assert "at=2026-08-31" in html
        assert "at=2026-09-28" in html
        assert "14 – 27 Sep 2026" in html

    def test_a_month_still_steps_by_months_not_by_days(self, shifts_client):
        html = shifts_client.get("/r/shifts?view=calendar&scale=month&at=2026-09-20").text
        assert "at=2026-08-01" in html
        assert "at=2026-10-01" in html
        assert "September 2026" in html

    def test_an_unknown_scale_falls_back_to_the_default(self, shifts_client):
        html = shifts_client.get("/r/shifts?view=calendar&scale=decade&at=2026-09-03").text
        assert "September 2026" in html

    def test_switching_scale_keeps_the_window_you_were_looking_at(self, shifts_client):
        # The scale links carry the window's own start, so moving from a month
        # to a week lands in that month rather than back on today.
        html = shifts_client.get("/r/shifts?view=calendar&scale=month&at=2026-09-20").text
        assert "scale=week&amp;at=2026-09-01" in html.replace("&#39;", "'")


class TestTheOlderMonthLinks:
    """`?year=&month=` shipped before the other scales. A bookmark still works."""

    def test_year_and_month_still_choose_the_window(self, shifts_client):
        html = shifts_client.get("/r/shifts?view=calendar&year=2026&month=9").text
        assert "September 2026" in html
        assert "Ada (lead)" in html

    def test_an_impossible_year_falls_back_to_today_rather_than_erroring(self, shifts_client):
        response = shifts_client.get("/r/shifts?view=calendar&year=0&month=9")
        assert response.status_code == 200

    def test_a_month_out_of_range_is_clamped(self, shifts_client):
        html = shifts_client.get("/r/shifts?view=calendar&year=2026&month=99").text
        assert "December 2026" in html
