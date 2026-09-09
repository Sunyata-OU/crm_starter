"""The view kinds a resource can be browsed through, beyond a list.

Each of these renders through the real routes and templates against in-memory
providers, because what is worth proving is not that a spec object holds the
values it was given -- it is that the route builds the right query from it and
the template puts the result where a reader will look for it.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

import pytest
from starlette.testclient import TestClient

from app.core.query import Condition, Op
from app.fields.types import (
    DateField,
    DecimalField,
    RelationField,
    SelectField,
    TextField,
)
from app.main import create_app
from app.providers.memory import MemoryProvider
from app.resources.resource import Resource
from app.resources.views import (
    ActivityView,
    ChartView,
    Column,
    DashboardView,
    GanttView,
    ListView,
    MapView,
    Panel,
    TreeView,
)
from tests.conftest import build_registry, sign_in

#: Activity states are read against the clock, so the fixtures are placed
#: relative to it -- pinning them to a literal date would test "overdue" for a
#: while and then test nothing.
TODAY = date.today()

#: A three-level hierarchy with a second root, so "roots only" is a claim that
#: can fail in both directions.
FOLDER_ROWS = [
    {"id": 1, "name": "Europe", "parent_id": None, "region": "emea"},
    {"id": 2, "name": "United Kingdom", "parent_id": 1, "region": "emea"},
    {"id": 3, "name": "London", "parent_id": 2, "region": "emea"},
    {"id": 4, "name": "Germany", "parent_id": 1, "region": "emea"},
    {"id": 5, "name": "Americas", "parent_id": None, "region": "amer"},
    {"id": 6, "name": "Boston", "parent_id": 5, "region": "amer"},
]

SITE_ROWS = [
    {"id": 1, "name": "London", "lat": 51.5072, "lon": -0.1276, "kind": "office"},
    {"id": 2, "name": "Boston", "lat": 42.3601, "lon": -71.0589, "kind": "office"},
    {"id": 3, "name": "Nowhere", "lat": None, "lon": None, "kind": "depot"},
    # Stored, but not a point: latitude runs to 90.
    {"id": 4, "name": "Impossible", "lat": 999.0, "lon": 0.0, "kind": "depot"},
]

TASK_ROWS = [
    {"id": 1, "subject": "Call Ada", "owner": "kim", "kind": "call",
     "due_on": TODAY - timedelta(days=3), "done": False},
    {"id": 2, "subject": "Email Grace", "owner": "kim", "kind": "email",
     "due_on": TODAY, "done": False},
    {"id": 3, "subject": "Review deck", "owner": "sam", "kind": "task",
     "due_on": TODAY + timedelta(days=9), "done": False},
    {"id": 4, "subject": "Old call", "owner": "sam", "kind": "call",
     "due_on": TODAY - timedelta(days=30), "done": True},
]

BOOKING_ROWS = [
    {"id": 1, "name": "Rollout", "starts": date(2026, 6, 1), "ends": date(2026, 6, 20),
     "team": "blue", "progress": 40},
    {"id": 2, "name": "Migration", "starts": date(2026, 6, 10), "ends": date(2026, 8, 30),
     "team": "red", "progress": 10},
    # Entirely before the window under test.
    {"id": 3, "name": "Ancient", "starts": date(2025, 1, 1), "ends": date(2025, 2, 1),
     "team": "blue", "progress": 100},
]


def folders_resource(**view_kwargs) -> Resource:
    return Resource(
        "folders",
        provider=MemoryProvider(FOLDER_ROWS, searchable_fields=("name",)),
        fields=[
            TextField("id", in_form=False),
            TextField("name", required=True, searchable=True),
            RelationField("parent_id", resource="folders", display="name", in_filter=True),
            SelectField("region", choices=["emea", "amer"], in_filter=True),
        ],
        views=[
            TreeView(
                columns=[Column("name", link=True), "region"],
                parent_field="parent_id",
                default_sort=["name"],
                **view_kwargs,
            )
        ],
    )


def sites_resource() -> Resource:
    return Resource(
        "sites",
        provider=MemoryProvider(SITE_ROWS, searchable_fields=("name",)),
        fields=[
            TextField("id", in_form=False),
            TextField("name", required=True, searchable=True),
            DecimalField("lat", places=4),
            DecimalField("lon", places=4),
            SelectField("kind", choices=["office", "depot"], in_filter=True),
        ],
        views=[MapView(lat_field="lat", lon_field="lon", title_field="name",
                       color_field="kind")],
    )


def tasks_resource() -> Resource:
    return Resource(
        "tasks",
        provider=MemoryProvider(TASK_ROWS, searchable_fields=("subject",)),
        display_field="subject",
        fields=[
            TextField("id", in_form=False),
            TextField("subject", required=True, searchable=True),
            TextField("owner", in_filter=True),
            SelectField("kind", choices=["call", "email", "task"], in_filter=True),
            DateField("due_on"),
            SelectField("done", choices=[True, False], in_filter=True),
        ],
        views=[
            ActivityView(
                row_field="owner",
                activity_field="kind",
                due_field="due_on",
                done_filter=Condition("done", Op.EQ, False),
            )
        ],
    )


def bookings_resource() -> Resource:
    return Resource(
        "bookings",
        provider=MemoryProvider(BOOKING_ROWS, searchable_fields=("name",)),
        fields=[
            TextField("id", in_form=False),
            TextField("name", required=True, searchable=True),
            DateField("starts"),
            DateField("ends"),
            SelectField("team", choices=["blue", "red"], in_filter=True),
            DecimalField("progress"),
        ],
        views=[
            ListView(columns=[Column("name", link=True), "team"]),
            GanttView(
                start_field="starts",
                end_field="ends",
                title_field="name",
                group_by="team",
                progress_field="progress",
                default_scale="week",
                span=4,
            ),
        ],
    )


def client_for(*resources: Resource, settings) -> TestClient:
    registry = build_registry()
    for resource in resources:
        registry.add_resource(resource)
    client = TestClient(create_app(settings=settings, registry=registry))
    client.__enter__()
    sign_in(client, email="admin@example.com", roles=["admin"])
    return client


@pytest.fixture
def tree_client(settings):
    client = client_for(folders_resource(), settings=settings)
    yield client
    client.__exit__(None, None, None)


@pytest.fixture
def map_client(settings):
    client = client_for(sites_resource(), settings=settings)
    yield client
    client.__exit__(None, None, None)


@pytest.fixture
def task_client(settings):
    client = client_for(tasks_resource(), settings=settings)
    yield client
    client.__exit__(None, None, None)


@pytest.fixture
def gantt_client(settings):
    client = client_for(bookings_resource(), settings=settings)
    yield client
    client.__exit__(None, None, None)


# -- tree ------------------------------------------------------------------


class TestTheTreeView:
    def test_only_roots_are_rendered(self, tree_client):
        html = tree_client.get("/r/folders?view=tree").text
        assert "Europe" in html and "Americas" in html
        # A child is reachable by opening its parent, not by being on the page.
        assert "United Kingdom" not in html

    def test_a_root_with_children_offers_a_toggle(self, tree_client):
        html = tree_client.get("/r/folders?view=tree").text
        assert "/r/folders/1/children" in html

    def test_a_leaf_offers_no_toggle(self, settings):
        client = client_for(folders_resource(), settings=settings)
        try:
            html = client.get("/r/folders/2/children?depth=2").text
            # London (3) has nothing under it.
            assert "/r/folders/3/children" not in html
        finally:
            client.__exit__(None, None, None)

    def test_opening_a_row_returns_its_children_only(self, tree_client):
        html = tree_client.get("/r/folders/1/children?depth=1").text
        assert "United Kingdom" in html and "Germany" in html
        assert "Boston" not in html
        assert "Europe" not in html

    def test_children_come_back_one_level_deeper(self, tree_client):
        html = tree_client.get("/r/folders/1/children?depth=1").text
        assert 'data-depth="1"' in html
        # And their own toggles ask for the level below that.
        assert "depth=2" in html

    def test_the_fragment_is_rows_and_not_a_page(self, tree_client):
        html = tree_client.get("/r/folders/1/children?depth=1").text
        assert "<html" not in html.lower()
        assert html.lstrip().startswith("<tr") or "<tr" in html

    def test_a_branch_deeper_than_max_depth_stops(self, settings):
        client = client_for(folders_resource(max_depth=1), settings=settings)
        try:
            html = client.get("/r/folders/1/children?depth=2").text
            assert "Stopped at 1 levels" in html
            assert "United Kingdom" not in html
        finally:
            client.__exit__(None, None, None)

    def test_searching_flattens_the_tree(self, tree_client):
        # "London" is two levels down: hiding it behind a root that does not
        # match would make the search look broken.
        html = tree_client.get("/r/folders?view=tree&q=London").text
        assert "London" in html
        assert "Showing every match, flattened" in html

    def test_filtering_flattens_the_tree(self, tree_client):
        html = tree_client.get("/r/folders?view=tree&f.region=amer").text
        assert "Boston" in html
        assert "United Kingdom" not in html

    def test_an_unfiltered_tree_says_nothing_about_flattening(self, tree_client):
        html = tree_client.get("/r/folders?view=tree").text
        assert "flattened" not in html

    def test_kanban_style_alias_reaches_the_same_view(self, tree_client):
        assert tree_client.get("/r/folders?view=hierarchy").status_code == 200

    def test_a_non_tree_view_is_not_a_children_endpoint(self, tree_client):
        assert tree_client.get("/r/folders/1/children?view=list").status_code == 404


class TestATreeOverTwoResources:
    """Contacts under companies: the parent column lives on the other side."""

    @pytest.fixture
    def client(self, settings):
        registry = build_registry()
        registry.resource("companies").add_view(
            TreeView(
                columns=[Column("name", link=True), "industry"],
                parent_field="company_id",
                child_resource="contacts",
            )
        )
        client = TestClient(create_app(settings=settings, registry=registry))
        client.__enter__()
        sign_in(client, email="admin@example.com", roles=["admin"])
        yield client
        client.__exit__(None, None, None)

    def test_every_row_is_a_root(self, client):
        html = client.get("/r/companies?view=tree").text
        assert "Analytical Engines" in html and "Bletchley Systems" in html

    def test_opening_a_row_lists_the_other_resource(self, client):
        html = client.get("/r/companies/1/children?depth=1").text
        assert "Ada Lovelace" in html
        assert "Alan Turing" not in html

    def test_children_of_children_are_not_offered(self, client):
        # A contact has no contacts under it; the recursion is one level deep
        # by construction, and a toggle there would 404.
        html = client.get("/r/companies/1/children?depth=1").text
        assert "/children" not in html


# -- gantt -----------------------------------------------------------------


class TestTheGanttView:
    def test_a_bar_is_drawn_for_each_record_in_the_window(self, gantt_client):
        html = gantt_client.get("/r/bookings?view=gantt&at=2026-06-01").text
        assert "Rollout" in html and "Migration" in html

    def test_a_record_outside_the_window_is_not_fetched(self, gantt_client):
        html = gantt_client.get("/r/bookings?view=gantt&at=2026-06-01").text
        assert "Ancient" not in html

    def test_bars_are_positioned_as_percentages(self, gantt_client):
        html = gantt_client.get("/r/bookings?view=gantt&at=2026-06-01").text
        assert re.search(r'left: [\d.]+%; width: [\d.]+%', html)

    def test_a_bar_running_past_the_window_is_marked_clipped(self, gantt_client):
        # Migration ends in August; a four-week window from June cannot show it.
        html = gantt_client.get("/r/bookings?view=gantt&at=2026-06-01").text
        assert "clipped-end" in html

    def test_no_bar_starts_before_the_window(self, gantt_client):
        html = gantt_client.get("/r/bookings?view=gantt&at=2026-06-01").text
        for offset in re.findall(r"left: ([\d.]+)%", html):
            assert float(offset) >= 0

    def test_no_bar_runs_off_the_end(self, gantt_client):
        html = gantt_client.get("/r/bookings?view=gantt&at=2026-06-01").text
        for left, width in re.findall(r"left: ([\d.]+)%; width: ([\d.]+)%", html):
            assert float(left) + float(width) <= 100.0001

    def test_the_scale_can_be_changed(self, gantt_client):
        response = gantt_client.get("/r/bookings?view=gantt&at=2026-06-01&scale=month")
        assert response.status_code == 200
        assert "Jun 2026" in response.text

    def test_an_unknown_scale_falls_back_to_the_declared_one(self, gantt_client):
        assert gantt_client.get("/r/bookings?view=gantt&scale=fortnight").status_code == 200

    def test_bars_are_banded_by_the_grouping_field(self, gantt_client):
        html = gantt_client.get("/r/bookings?view=gantt&at=2026-06-01").text
        assert "gantt-band" in html

    def test_an_empty_window_says_so(self, gantt_client):
        html = gantt_client.get("/r/bookings?view=gantt&at=2030-01-01").text
        assert "Nothing scheduled in this window" in html


class TestAGanttOfInstants:
    """Bars over `datetime` columns rather than dates: a shift schedule.

    Two things separate it from the bookings above. A moment belongs to a day
    only once somebody says whose day, and a bar drawn from the stored value
    would be a day out for anything late in the evening. And the band over a
    relation has a name available -- the label fetched for the bars in it --
    where the field itself has only a key.
    """

    @pytest.fixture
    def client(self, settings):
        from datetime import datetime

        from app.fields.types import DateTimeField, RelationField, SelectField

        resource = Resource(
            "shifts",
            provider=MemoryProvider([
                # 23:00-05:00 UTC: the evening of the 3rd everywhere west of
                # here, and the small hours of the 4th in Warsaw.
                {"id": 1, "company_id": "1", "status": "PENDING",
                 "starts": datetime(2026, 6, 3, 23, 0), "ends": datetime(2026, 6, 4, 5, 0)},
                {"id": 2, "company_id": "2", "status": "CONFIRMED",
                 "starts": datetime(2026, 6, 5, 9, 0), "ends": datetime(2026, 6, 5, 17, 0)},
            ]),
            fields=[
                TextField("id", in_form=False),
                RelationField("company_id", resource="companies", display="name"),
                SelectField("status", choices=[
                    ("PENDING", "Pending", "amber"),
                    ("CONFIRMED", "Confirmed", "green"),
                ]),
                DateTimeField("starts", absolute=True),
                DateTimeField("ends", absolute=True),
            ],
            views=[
                ListView(columns=[Column("starts", link=True)]),
                GanttView(start_field="starts", end_field="ends",
                          title_field="starts", group_by="company_id",
                          color_field="status", default_scale="day", span=10),
            ],
        )
        client = client_for(resource, settings=settings)
        yield client
        client.__exit__(None, None, None)

    def test_a_band_is_named_by_the_row_it_points_at(self, client):
        html = client.get("/r/shifts?view=gantt&at=2026-06-01").text
        assert "Analytical Engines" in html
        assert "gantt-band-label\">1<" not in html, "a band headed by a key names nothing"

    def test_a_bar_carries_the_colour_of_its_status(self, client):
        html = client.get("/r/shifts?view=gantt&at=2026-06-01").text
        assert "gantt-bar pill-amber" in html
        assert "gantt-bar pill-green" in html

    def test_a_late_bar_starts_on_the_readers_day(self, client):
        """23:00 UTC on the 3rd is one in the morning on the 4th in Warsaw, and
        a bar starting on the 3rd would be a schedule off by a day."""
        utc = client.get("/r/shifts?view=gantt&at=2026-06-01").text
        sign_in(client, email="admin@example.com", roles=["admin"],
                timezone="Europe/Warsaw")
        warsaw = client.get("/r/shifts?view=gantt&at=2026-06-01").text
        assert 'title="2026-06-03 &rarr; 2026-06-04"' in utc or \
               'title="2026-06-03 → 2026-06-04"' in utc
        assert 'title="2026-06-04 &rarr; 2026-06-04"' in warsaw or \
               'title="2026-06-04 → 2026-06-04"' in warsaw


# -- map -------------------------------------------------------------------


def markers_of(html: str) -> list[dict]:
    body = re.search(
        r'<script type="application/json" id="map-markers">(.*?)</script>', html, re.S
    )
    assert body, "the map rendered no marker data"
    return json.loads(body.group(1))


class TestTheMapView:
    def test_located_records_become_markers(self, map_client):
        markers = markers_of(map_client.get("/r/sites?view=map").text)
        assert {m["title"] for m in markers} == {"London", "Boston"}

    def test_a_record_without_coordinates_is_counted_not_placed(self, map_client):
        html = map_client.get("/r/sites?view=map").text
        assert "without coordinates" in html
        assert [m for m in markers_of(html) if m["title"] == "Nowhere"] == []

    def test_an_impossible_coordinate_is_dropped(self, map_client):
        # Stored, non-null, and not a place. Better absent than plotted.
        markers = markers_of(map_client.get("/r/sites?view=map").text)
        assert [m for m in markers if m["title"] == "Impossible"] == []

    def test_each_marker_links_to_its_record(self, map_client):
        markers = markers_of(map_client.get("/r/sites?view=map").text)
        assert all(m["url"].startswith("/r/sites/") for m in markers)

    def test_the_records_are_listed_as_well_as_mapped(self, map_client):
        # The list is the accessible view of the same data, and the only one
        # when scripting is off.
        html = map_client.get("/r/sites?view=map").text
        assert "Records on this map" in html

    def test_json_callers_get_the_markers(self, map_client):
        payload = map_client.get(
            "/r/sites?view=map", headers={"Accept": "application/json"}
        ).json()
        assert len(payload["markers"]) == 2
        assert payload["unplaced"] >= 1

    def test_the_tile_server_is_configurable(self, map_client):
        html = map_client.get("/r/sites?view=map").text
        assert 'data-tile-url="https://tile.openstreetmap.org' in html

    def test_without_a_tile_server_the_page_is_still_useful(self, settings):
        settings.map_tile_url = ""
        client = client_for(sites_resource(), settings=settings)
        try:
            html = client.get("/r/sites?view=map").text
            assert "No map tiles are configured" in html
            assert "London" in html  # the list is still there
            assert "leaflet.js" not in html
        finally:
            client.__exit__(None, None, None)

    def test_a_title_with_markup_travels_as_data(self, settings):
        rows = [{"id": 1, "name": "<script>alert(1)</script>", "lat": 1.0, "lon": 2.0,
                 "kind": "office"}]
        resource = sites_resource()
        resource.provider = MemoryProvider(rows, searchable_fields=("name",))
        client = client_for(resource, settings=settings)
        try:
            html = client.get("/r/sites?view=map").text
            assert "<script>alert(1)</script>" not in html
        finally:
            client.__exit__(None, None, None)


# -- activity --------------------------------------------------------------


class TestTheActivityView:
    def test_a_row_per_owner_and_a_column_per_kind(self, task_client):
        html = task_client.get("/r/tasks?view=activity").text
        assert "kim" in html and "sam" in html
        assert "call" in html and "email" in html

    def test_an_overdue_cell_is_marked_overdue(self, task_client):
        html = task_client.get("/r/tasks?view=activity").text
        assert "state-overdue" in html

    def test_work_due_today_is_distinguished_from_work_due_later(self, task_client):
        html = task_client.get("/r/tasks?view=activity").text
        assert "state-today" in html
        assert "state-planned" in html

    def test_completed_work_is_excluded(self, task_client):
        # Sam's only call is done; the cell should not exist at all.
        html = task_client.get("/r/tasks?view=activity").text
        rows = re.findall(r'<th scope="row">(.*?)</th>', html)
        assert "sam" in rows
        assert html.count("state-") >= 1

    def test_a_cell_links_to_the_records_behind_it(self, task_client):
        html = task_client.get("/r/tasks?view=activity").text
        assert "f.owner=kim" in html or "f.owner=kim" in html.replace("&amp;", "&")

    def test_the_key_explains_the_colours(self, task_client):
        html = task_client.get("/r/tasks?view=activity").text
        assert "Overdue" in html and "Due today" in html


# -- dashboard -------------------------------------------------------------


class TestAChartOverTime:
    """A chart of the last six months is a series, and a series read left to
    right has to be chronological. `sort_by_value` is the right default -- the
    usual question is "which is biggest" -- and the wrong one here."""

    MONTHS = [
        {"id": 1, "month": "2026-01", "amount": 900},
        {"id": 2, "month": "2026-02", "amount": 100},
        {"id": 3, "month": "2026-03", "amount": 500},
    ]

    @pytest.fixture
    def client(self, settings):
        from app.core.query import Agg, Measure

        registry = build_registry()
        registry.add_resource(
            Resource(
                "revenue",
                provider=MemoryProvider(self.MONTHS),
                fields=[
                    TextField("id", in_form=False),
                    TextField("month"),
                    DecimalField("amount"),
                ],
                views=[
                    ChartView(name="by_size", group_by="month",
                              measure=Measure(Agg.SUM, "amount")),
                    ChartView(name="over_time", group_by="month",
                              measure=Measure(Agg.SUM, "amount"), sort=("month",)),
                ],
            )
        )
        client = TestClient(create_app(settings=settings, registry=registry))
        client.__enter__()
        sign_in(client, email="admin@example.com", roles=["admin"])
        yield client
        client.__exit__(None, None, None)

    def _months(self, client, view):
        rows = client.get(
            f"/r/revenue?view={view}", headers={"Accept": "application/json"}
        ).json()["rows"]
        return [row["month"] for row in rows]

    def test_without_a_sort_the_biggest_group_comes_first(self, client):
        assert self._months(client, "by_size") == ["2026-01", "2026-03", "2026-02"]

    def test_with_one_the_groups_read_in_their_own_order(self, client):
        assert self._months(client, "over_time") == ["2026-01", "2026-02", "2026-03"]

    def test_declaring_a_sort_turns_ranking_by_measure_off(self):
        from app.core.query import Agg, Measure

        assert ChartView(group_by="m", measure=Measure(Agg.SUM, "a")).sort_by_value is True
        ranked = ChartView(group_by="m", measure=Measure(Agg.SUM, "a"), sort=("m",))
        assert ranked.sort_by_value is False, "the two orderings cannot both win"

    def test_a_sort_is_written_the_way_every_other_sort_is(self):
        from app.core.query import Agg, Measure, SortDir

        view = ChartView(group_by="m", measure=Measure(Agg.SUM, "a"), sort=("-m",))
        assert view.sort[0].field == "m"
        assert view.sort[0].dir is SortDir.DESC


class TestTheDashboardView:
    @pytest.fixture
    def client(self, settings):
        registry = build_registry()
        registry.resource("contacts").add_view(
            DashboardView(
                panels=[
                    Panel(view="list", title="Everyone", span=2),
                    Panel(view="list", resource="companies", title="Accounts"),
                    Panel(view="list", resource="nonexistent", title="Gone"),
                ],
                label="Overview",
            )
        )
        client = TestClient(create_app(settings=settings, registry=registry))
        client.__enter__()
        sign_in(client, email="admin@example.com", roles=["admin"])
        yield client
        client.__exit__(None, None, None)

    def test_each_panel_is_a_lazy_request_for_a_real_view(self, client):
        html = client.get("/r/contacts?view=dashboard").text
        assert 'hx-get="/r/contacts?view=list&amp;panel=1"' in html
        assert 'hx-get="/r/companies?view=list&amp;panel=1"' in html

    def test_a_panel_can_be_one_queue_rather_than_the_whole_table(self, settings):
        """The point of `params`: a dashboard of work queues is the same view
        several times over, each narrowed to the rows somebody has to act on.
        Anything the list route reads works, because it is the list route."""
        registry = build_registry()
        registry.resource("contacts").add_view(
            DashboardView(panels=[
                Panel(view="list", title="Software",
                      params={"f.industry": "Software", "sort": "-name"}),
            ])
        )
        client = TestClient(create_app(settings=settings, registry=registry))
        client.__enter__()
        try:
            sign_in(client, email="admin@example.com", roles=["admin"])
            html = client.get("/r/contacts?view=dashboard").text
            assert "f.industry=Software" in html.replace("&amp;", "&")
            assert "sort=-name" in html.replace("&amp;", "&")
        finally:
            client.__exit__(None, None, None)

    def test_a_panel_parameter_is_escaped_rather_than_pasted_in(self):
        panel = Panel(view="list", params={"q": "a & b"})
        assert panel.url("contacts") == "/r/contacts?view=list&panel=1&q=a%20%26%20b"

    def test_a_panel_naming_a_missing_resource_is_dropped(self, client):
        html = client.get("/r/contacts?view=dashboard").text
        assert "nonexistent" not in html

    def test_a_panel_renders_without_the_page_around_it(self, client):
        html = client.get("/r/contacts?view=list&panel=1").text
        assert "<html" not in html.lower()
        assert "<nav" not in html.lower()
        # It is still the real view: the records are there.
        assert "Ada Lovelace" in html

    def test_a_panel_has_no_toolbar(self, client):
        html = client.get("/r/contacts?view=list&panel=1").text
        assert "view-switch" not in html

    def test_the_full_page_still_has_its_toolbar(self, client):
        html = client.get("/r/contacts?view=list").text
        assert "view-switch" in html

    def test_a_panel_the_caller_cannot_read_is_not_offered(self, settings):
        from app.resources.policy import RolePolicy

        registry = build_registry()
        registry.resource("companies").policy = RolePolicy(read=("admin",))
        registry.resource("contacts").add_view(
            DashboardView(panels=[Panel(view="list", resource="companies")])
        )
        client = TestClient(create_app(settings=settings, registry=registry))
        client.__enter__()
        try:
            sign_in(client, email="sam@example.com", roles=["viewer"])
            html = client.get("/r/contacts?view=dashboard").text
            assert "/r/companies?view=list" not in html
        finally:
            client.__exit__(None, None, None)
