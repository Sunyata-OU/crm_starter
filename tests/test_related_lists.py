"""Embedded lists that do more than list.

Three things a related block on a detail page could not do, each of which
sent the reader somewhere else to do it:

* run the target resource's row actions -- taking somebody off a shift from
  the shift they are on, rather than from their own page;
* come back to the page the button was pressed on rather than to the row it
  acted on;
* nest, so a person's jobs can carry their shifts underneath instead of
  presenting sixty rows in no particular grouping.

The registry here is built for the test: the behaviour belongs to the
framework, and a test that needed live databases to prove it would not run.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from starlette.testclient import TestClient

from app.core.registry import Registry
from app.fields.types import (
    BackrefField,
    DateTimeField,
    IntegerField,
    TextAreaField,
    TextField,
)
from app.main import create_app
from app.providers.memory import MemoryProvider
from app.resources.actions import ActionResult, action
from app.resources.resource import Resource
from app.resources.views import Column, DetailView, ListView, Section, TreeView
from app.settings import Settings
from tests.conftest import csrf_from, sign_in

#: Long enough that a table cell shortens it, which is what the detail page
#: must not do -- the screen somebody opened to read it.
NOTES = (
    "Otsime osavat ehitajat, et tagada meie ehitusprojektide sujuv kulg, "
    "alates vundamendist kuni viimistluseni, meeskonnas kes teab mida teeb."
)

VENUES = [{"id": 1, "name": "Old Mill", "notes": NOTES}]

#: One venue, two runs, three bookings -- enough for a tree with a branch that
#: is not the only branch.
RUNS = [
    {"key": "1-spring", "venue_id": 1, "name": "Spring run"},
    {"key": "1-autumn", "venue_id": 1, "name": "Autumn run"},
]

BOOKINGS = [
    {"id": 1, "venue_id": 1, "run_key": "1-spring", "reference": "SP-1",
     "starts_at": datetime(2026, 4, 1, 9, 0), "released": False},
    {"id": 2, "venue_id": 1, "run_key": "1-spring", "reference": "SP-2",
     "starts_at": datetime(2026, 4, 2, 9, 0), "released": False},
    {"id": 3, "venue_id": 1, "run_key": "1-autumn", "reference": "AU-1",
     "starts_at": datetime(2026, 10, 1, 9, 0), "released": True},
]


@action(
    "release", "Release", style="danger", placements=("row", "detail"),
    available=lambda record, identity: not record.get("released"),
)
async def release(records, ctx, resource) -> ActionResult:
    return ActionResult(message="Released.")


def build_registry() -> Registry:
    registry = Registry()
    registry.add_resource(
        Resource(
            "bookings",
            provider=MemoryProvider(BOOKINGS),
            display_field="reference",
            actions=[release],
            fields=[
                IntegerField("id", in_list=False),
                IntegerField("venue_id", in_list=False),
                TextField("run_key", in_list=False),
                TextField("reference"),
                # The field under test in `TestAnAppointmentReadsAsADate`.
                DateTimeField("starts_at", label="Starts", absolute=True),
            ],
            views=[ListView(columns=[Column("reference"), "starts_at"])],
        )
    )
    registry.add_resource(
        Resource(
            "runs",
            provider=MemoryProvider(RUNS, pk_field="key"),
            pk="key",
            display_field="name",
            fields=[
                TextField("key", in_list=False),
                IntegerField("venue_id", in_list=False),
                TextField("name"),
            ],
            views=[
                ListView(columns=[Column("name")]),
                TreeView(columns=[Column("name")], child_resource="bookings",
                         parent_field="run_key", row_link=False),
            ],
        )
    )
    registry.add_resource(
        Resource(
            "venues",
            provider=MemoryProvider(VENUES),
            display_field="name",
            fields=[
                IntegerField("id", in_list=False),
                TextField("name"),
                TextAreaField("notes", rows=4),
                BackrefField("bookings", resource="bookings", via="venue_id",
                             label="Bookings", actions=True,
                             columns=("reference", "starts_at")),
                # The same rows in the order this screen reads them in, which
                # is not the order the bookings screen does.
                BackrefField("latest", resource="bookings", via="venue_id",
                             label="Latest", order=("-starts_at",),
                             columns=("reference", "starts_at")),
                BackrefField("runs", resource="runs", via="venue_id",
                             label="Runs", tree="tree"),
            ],
            views=[
                ListView(columns=[Column("name"), "notes"]),
                DetailView(sections=[
                    Section("Notes", ["notes"], columns=1),
                    Section("Bookings", ["bookings"], columns=1),
                    Section("Latest", ["latest"], columns=1),
                    Section("Runs", ["runs"], columns=1),
                ], timeline=False),
            ],
        )
    )
    return registry


@pytest.fixture
def client() -> TestClient:
    settings = Settings(
        environment="test",
        secret_key="test-key-not-for-real-use",
        template_reload=False,
        auth_providers=["session"],
        modules=[],
    )
    app = create_app(settings=settings, registry=build_registry())
    with TestClient(app, raise_server_exceptions=False) as c:
        sign_in(c, email="admin@example.com", roles=["admin"])
        yield c


def _run(client: TestClient, url: str):
    """Press an action button the way the page does -- htmx, with the token the
    layout puts on every request."""
    return client.post(
        url,
        headers={"HX-Request": "true", "X-CSRF-Token": csrf_from(client, "/r/venues/1")},
        follow_redirects=False,
    )


@pytest.fixture
def venue(client) -> str:
    return client.get("/r/venues/1").text


class TestARowActionIsOfferedWhereTheRowIs:
    def test_the_embedded_list_offers_the_targets_row_actions(self, venue):
        assert "/r/bookings/1/action/release" in venue

    def test_it_comes_back_to_the_page_it_was_pressed_on(self, venue):
        """Not to `/r/bookings/1`, which is a screen nobody asked to see."""
        assert "back=/r/venues/1" in venue

    def test_a_row_the_action_would_not_help_does_not_offer_it(self, venue):
        """Booking 3 is already released. The availability rule is the target
        resource's own -- this list does not re-implement it."""
        assert "/r/bookings/3/action/release" not in venue

    def test_a_backref_that_did_not_ask_for_actions_has_none(self, client):
        """Off by default: an embedded list is usually context, and buttons
        appearing because another resource gained an action is nobody's
        design."""
        registry = client.app.state.crm.registry
        assert not registry.resource("venues").field("runs").actions

    def test_the_action_returns_to_where_the_caller_asked(self, client):
        response = _run(client, "/r/bookings/1/action/release?back=/r/venues/1")
        assert response.headers.get("HX-Redirect") == "/r/venues/1"

    @pytest.mark.parametrize("target", ["https://elsewhere.test/", "//elsewhere.test/"])
    def test_an_off_site_return_is_dropped_rather_than_followed(self, client, target):
        """A redirect target that arrives in a query string is an open redirect
        wearing a helpful name. `//host` counts: the browser reads it as a URL
        with a host and no scheme."""
        response = _run(client, f"/r/bookings/1/action/release?back={target}")
        assert response.headers.get("HX-Redirect") == "/r/bookings/1"


class TestAnEmbeddedListCanNest:
    def test_the_roots_are_drawn_with_expanders(self, venue):
        assert "Spring run" in venue
        assert "/r/runs/1-spring/children" in venue

    def test_a_branch_arrives_from_the_targets_own_fragment(self, client):
        branch = client.get("/r/runs/1-spring/children?view=tree&depth=1").text
        assert "SP-1" in branch and "SP-2" in branch
        assert "AU-1" not in branch, "a branch shows its own children only"

    def test_a_flat_backref_is_still_flat(self, venue):
        """Both shapes on one page: the bookings list is not a tree."""
        assert "/r/bookings/1/children" not in venue


class TestAnAppointmentReadsAsADate:
    def test_a_scheduled_instant_shows_its_date_and_time(self, venue):
        """Not "in 2 months", which is the one thing nobody can plan from."""
        assert "01 Apr 2026, 09:00" in venue

    def test_the_distance_moves_into_the_tooltip(self, venue):
        """Kept, rather than dropped: "is that this week?" is still a question
        the column should answer at a glance."""
        assert 'title="in ' in venue or 'title="' in venue


class TestThePageIsHeadedByWhatItIsAbout:
    """``display_field`` is what a *link* says; a detail view naming a
    ``title_field`` heads the page with that instead. The setting was declared
    on every view here and read by none of them."""

    def test_the_title_field_heads_the_page(self, client):
        registry = client.app.state.crm.registry
        registry.resource("bookings").view("detail").extend(title_field="starts_at")
        assert "01 Apr 2026, 09:00" in client.get("/r/bookings/1").text

    def test_it_falls_back_to_the_link_name(self, client):
        """A view may name a field this caller cannot see, and a heading is not
        worth a 500."""
        registry = client.app.state.crm.registry
        registry.resource("bookings").view("detail").extend(title_field="nonexistent")
        assert "SP-1" in client.get("/r/bookings/1").text


class TestAnEmbeddedListReadsInTheOrderThatPageNeeds:
    """Which order is right depends on the record you arrived from. A booking
    list is a booking list; a *venue's* bookings are read latest first, and
    only the backref knows that."""

    def test_the_backref_orders_the_rows_it_embeds(self, venue):
        latest = venue.split('id="related-latest"')[1]
        assert latest.index("AU-1") < latest.index("SP-2") < latest.index("SP-1")

    def test_a_backref_without_one_takes_the_targets_default(self, venue):
        block = venue.split('id="related-bookings"')[1].split('id="related-latest"')[0]
        assert block.index("SP-1") < block.index("SP-2") < block.index("AU-1")


class TestALongTextIsWholeWhereThereIsRoomForIt:
    """A description read as "Otsime osavat ehitajat, et tagada meie ehitusp"
    on the page opened to read it: the cell rule -- one line, ellipsis --
    applied on the detail page too."""

    def test_the_detail_page_shows_all_of_it(self, venue):
        assert NOTES in venue
        assert "…" not in venue.split("Bookings")[0]

    def test_a_table_cell_still_shortens_it(self, client):
        """The whole of it stays in the tooltip; the cell shows one line."""
        listing = client.get("/r/venues").text
        assert f">{NOTES}<" not in listing
        assert "meeskonnas…" in listing
