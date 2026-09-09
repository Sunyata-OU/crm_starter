"""A calendar names its events through the field, like every other view.

The failure this guards against is quiet: a computed title has nothing stored
under its key, so a raw ``record.get`` returns None and the grid places every
event correctly with the label "None". Nothing errors, so only a test that
looks at what the chip actually says will catch it.
"""

from __future__ import annotations

import re
from datetime import date, datetime

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


class TestACalendarOfInstants:
    """The other kind of calendar: blocks of time rather than dated rows.

    A shift is stored in UTC and read by somebody in Warsaw or Tallinn, which
    makes three things the grid has to get right: which day a block lands on,
    what clock time is printed inside it, and whether the chips above it have
    any effect on what is drawn at all.
    """

    ROWS = [
        # Late on the 3rd in UTC, which is already the 4th anywhere in Europe.
        {"id": 1, "job": "Bartender", "status": "PENDING",
         "start": datetime(2026, 9, 3, 23, 30), "end": datetime(2026, 9, 4, 6, 0)},
        {"id": 2, "job": "Bartender", "status": "CONFIRMED",
         "start": datetime(2026, 9, 10, 9, 0), "end": datetime(2026, 9, 10, 17, 0)},
        {"id": 3, "job": "Cleaner", "status": "CANCELLED",
         "start": datetime(2026, 9, 10, 12, 0), "end": datetime(2026, 9, 10, 20, 0)},
    ]

    @pytest.fixture
    def client(self, settings):
        from app.core.query import Condition, Op
        from app.fields.types import DateTimeField, SelectField
        from app.resources.views import QuickFilter, SearchSpec

        registry = build_registry()
        registry.add_resource(
            Resource(
                "blocks",
                provider=MemoryProvider(self.ROWS),
                display_field="job",
                fields=[
                    TextField("id", in_form=False),
                    TextField("job"),
                    SelectField("status", choices=[
                        ("PENDING", "Pending", "amber"),
                        ("CONFIRMED", "Confirmed", "green"),
                        ("CANCELLED", "Cancelled", "red"),
                    ], in_filter=True),
                    DateTimeField("start", absolute=True),
                    DateTimeField("end", absolute=True),
                ],
                search=SearchSpec(quick_filters=[
                    QuickFilter("cancelled", "Cancelled",
                                build=lambda identity: Condition("status", Op.EQ, "CANCELLED")),
                ]),
                views=[CalendarView(start_field="start", end_field="end",
                                    title_field="job", color_field="job",
                                    all_day=False, default_scale="month")],
            )
        )
        with TestClient(create_app(settings=settings, registry=registry)) as c:
            sign_in(c, email="admin@example.com", roles=["admin"],
                    timezone="Europe/Warsaw")
            yield c

    def page(self, client, **params) -> str:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return client.get(f"/r/blocks?view=calendar&{query}").text

    def cell(self, html: str, day: int) -> str:
        """The markup of one day cell, so an assertion cannot match its neighbour."""
        cells = html.split('<td class="cal-day')
        for chunk in cells[1:]:
            date_line = chunk.split("</div>", 1)[0]
            if f">{day}</a>" in date_line or f">{day}<" in date_line.split("cal-date")[-1]:
                return chunk
        raise AssertionError(f"no cell for day {day}")

    def test_a_late_shift_is_placed_on_the_day_it_starts_where_the_reader_is(self, client):
        """23:30 UTC on the 3rd is half past midnight on the 4th in Warsaw."""
        html = self.page(client, year=2026, month=9)
        assert "Bartender" in self.cell(html, 4)
        assert "Bartender" not in self.cell(html, 3)

    def test_the_clock_time_in_the_block_is_the_readers_own(self, client):
        html = self.page(client, year=2026, month=9)
        assert "01:30" in self.cell(html, 4), "Warsaw is two hours ahead in September"
        assert "23:30" not in html, "which is the stored value, not a readable one"

    def test_two_shifts_on_the_same_job_share_a_colour(self, client):
        html = self.page(client, year=2026, month=9)
        tones = re.findall(r'class="cal-event (hue-\d+)"', html)
        assert len(tones) == 3
        assert tones[0] == tones[1], "the same job, twice"
        assert tones[2] != tones[0], "a different job"

    def test_a_quick_filter_narrows_the_grid(self, client):
        """The chips are drawn above this view, so they have to act on it."""
        html = self.page(client, year=2026, month=9, quick="cancelled")
        assert "Cleaner" in html
        assert "Bartender" not in html

    def test_a_filter_narrows_it_too(self, client):
        html = self.page(client, year=2026, month=9, **{"f.status": "CONFIRMED"})
        assert "Cleaner" not in html

    def test_a_week_is_seven_cells_and_the_month_around_it_is_not_drawn(self, client):
        html = self.page(client, scale="week", at="2026-09-10")
        assert html.count('<td class="cal-day') == 7
        assert "Bartender" in html and "Cleaner" in html
        # Both ends of a block are the reader's: 09:00-17:00 UTC read in Warsaw.
        assert "11:00" in html and "19:00" in html

    def test_the_scale_the_view_declares_is_what_it_opens_on(self, settings):
        from app.fields.types import DateTimeField

        registry = build_registry()
        registry.add_resource(
            Resource(
                "weekly",
                provider=MemoryProvider(self.ROWS),
                display_field="job",
                fields=[TextField("id", in_form=False), TextField("job"),
                        DateTimeField("start", absolute=True)],
                views=[CalendarView(start_field="start", title_field="job",
                                    default_scale="week")],
            )
        )
        with TestClient(create_app(settings=settings, registry=registry)) as c:
            sign_in(c, email="admin@example.com", roles=["admin"])
            html = c.get("/r/weekly?view=calendar").text
        assert html.count('<td class="cal-day') == 7


class TestTheTitleOfARelation:
    """A block naming a key is a block nobody can read. The label is fetched
    for the page anyway -- resolving relations is what every other view does --
    so the calendar uses it."""

    @pytest.fixture
    def client(self, settings):
        from app.fields.types import DateTimeField, RelationField

        registry = build_registry()
        registry.add_resource(
            Resource(
                "engagements",
                provider=MemoryProvider([
                    {"id": 1, "company_id": "1", "start": datetime(2026, 9, 8, 9, 0)},
                ]),
                fields=[
                    TextField("id", in_form=False),
                    RelationField("company_id", resource="companies", display="name"),
                    DateTimeField("start", absolute=True),
                ],
                views=[CalendarView(start_field="start", title_field="company_id")],
            )
        )
        with TestClient(create_app(settings=settings, registry=registry)) as c:
            sign_in(c, email="admin@example.com", roles=["admin"])
            yield c

    def test_the_block_is_named_by_the_row_it_points_at(self, client):
        html = client.get("/r/engagements?view=calendar&year=2026&month=9").text
        assert "Analytical Engines" in html
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
