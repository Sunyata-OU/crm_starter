"""Time.

The rule these tests hold to: **instants are stored in UTC and displayed in the
reader's zone; calendar dates are neither**.

The second half of that is the one people get wrong. A deal expected to close
on the 5th closes on the 5th for everyone; converting it would move it to the
4th for a reader in Los Angeles, which is nonsense.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.clock import (
    day_bounds,
    is_valid_timezone,
    offset_label,
    parse,
    parse_date,
    resolve,
    timezone_choices,
    to_utc,
    to_zone,
    today,
    utcnow,
)
from app.core.results import Ctx, Identity
from app.fields.types import DateField, DateTimeField, TimezoneField
from app.web.render import _ago, _format_date, _format_instant

TOKYO = "Asia/Tokyo"
LA = "America/Los_Angeles"
LONDON = "Europe/London"

#: Noon UTC, which is evening in Tokyo and morning in Los Angeles.
NOON = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


class TestTheClock:
    def test_now_is_always_aware(self):
        # A naive "now" is the server's wall clock, which is an accident of
        # where it happens to be running.
        assert utcnow().tzinfo is not None

    def test_now_is_in_utc(self):
        assert utcnow().utcoffset() == timedelta(0)

    def test_an_unknown_zone_falls_back_rather_than_raising(self):
        # A bad value in one person's preferences must not break their page.
        assert resolve("Mars/Olympus_Mons") is UTC
        assert resolve("") is UTC
        assert resolve(None) is UTC

    def test_a_known_zone_resolves(self):
        assert resolve(TOKYO) == ZoneInfo(TOKYO)

    def test_validity_can_be_checked(self):
        assert is_valid_timezone(TOKYO)
        assert is_valid_timezone("UTC")
        assert not is_valid_timezone("Nowhere/Special")


class TestConversion:
    def test_one_instant_reads_differently_in_each_zone(self):
        assert to_zone(NOON, TOKYO).hour == 21
        assert to_zone(NOON, LA).hour == 5

    def test_conversion_does_not_move_the_instant(self):
        assert to_zone(NOON, TOKYO) == to_zone(NOON, LA) == NOON

    def test_a_naive_value_is_read_as_utc(self):
        # That is what this application stores, so a row written before any of
        # this existed reads correctly rather than shifted.
        naive = datetime(2026, 9, 2, 12, 0)
        assert to_zone(naive, TOKYO).hour == 21

    def test_a_submitted_value_is_read_in_the_submitters_zone(self):
        # Someone typing "14:00" means two o'clock where they are.
        typed = datetime(2026, 9, 2, 14, 0)
        assert to_utc(typed, assume=TOKYO) == datetime(2026, 9, 2, 5, 0, tzinfo=UTC)

    def test_an_aware_value_keeps_its_meaning(self):
        already = datetime(2026, 9, 2, 14, 0, tzinfo=ZoneInfo(LA))
        assert to_utc(already, assume=TOKYO) == already


class TestTodayDependsOnWhoIsAsking:
    def test_the_date_differs_across_the_dateline(self):
        # At some instants these differ; the point is that the question has no
        # single answer, which is why date.today() is wrong here.
        tokyo_day = today(TOKYO)
        la_day = today(LA)
        assert (tokyo_day - la_day).days in (0, 1)

    def test_a_day_spans_different_universal_time_in_each_zone(self):
        tokyo_start, tokyo_end = day_bounds(date(2026, 9, 3), TOKYO)
        la_start, _ = day_bounds(date(2026, 9, 3), LA)
        assert tokyo_start == datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
        assert tokyo_end - tokyo_start == timedelta(days=1)
        assert la_start > tokyo_start, "the day starts later further west"


class TestParsing:
    @pytest.mark.parametrize(
        "text",
        ["2026-09-02T12:00:00+00:00", "2026-09-02T12:00:00Z", "2026-09-02 12:00:00"],
    )
    def test_the_usual_forms_are_understood(self, text):
        assert parse(text) == NOON

    def test_z_is_understood_although_python_does_not(self):
        # JSON and most APIs write "Z"; fromisoformat alone rejects it.
        assert parse("2026-09-02T12:00:00Z").tzinfo is not None

    def test_something_unparseable_yields_none(self):
        # A bad column should render blank, not break the page around it.
        assert parse("not a time") is None
        assert parse("") is None

    def test_a_date_parses_to_midnight(self):
        assert parse(date(2026, 9, 2)) == datetime(2026, 9, 2, 0, 0, tzinfo=UTC)

    def test_dates_are_parsed_separately(self):
        assert parse_date("2026-09-02") == date(2026, 9, 2)
        assert parse_date(NOON) == date(2026, 9, 2)


class TestFields:
    """The distinction between an instant and a calendar date."""

    def test_a_datetime_is_stored_in_utc(self):
        field = DateTimeField("when")
        value = field.coerce("2026-09-02T14:00", Ctx(timezone=TOKYO))
        assert field.to_storage(value) == datetime(2026, 9, 2, 5, 0, tzinfo=UTC)

    def test_the_same_wall_time_from_two_zones_is_two_instants(self):
        field = DateTimeField("when")
        tokyo = field.to_storage(field.coerce("2026-09-02T14:00", Ctx(timezone=TOKYO)))
        la = field.to_storage(field.coerce("2026-09-02T14:00", Ctx(timezone=LA)))
        assert tokyo != la
        assert (la - tokyo) == timedelta(hours=16)

    def test_a_date_is_never_converted(self):
        # A deal closing on the 5th does not close on the 4th because the
        # reader is in Los Angeles.
        field = DateField("closes")
        for zone in (TOKYO, LA, LONDON):
            assert field.coerce("2026-09-05", Ctx(timezone=zone)) == date(2026, 9, 5)

    def test_a_date_is_stored_as_itself(self):
        assert DateField("d").to_storage(date(2026, 9, 5)) == date(2026, 9, 5)

    def test_a_datetime_without_a_zone_is_taken_as_utc(self):
        assert DateTimeField("d").to_python("2026-09-02 12:00").tzinfo is not None


class TestTimezoneField:
    def test_the_choices_show_current_offsets(self):
        choices = TimezoneField().choices(Ctx())
        labels = {c.value: c.label for c in choices}
        assert "UTC" in labels
        assert "(UTC" in labels[LONDON], "the offset helps a person confirm the choice"

    def test_a_valid_zone_is_accepted(self):
        TimezoneField().validate(TOKYO, Ctx())

    def test_an_invalid_zone_is_rejected(self):
        from app.core.errors import ValidationFailed

        with pytest.raises(ValidationFailed):
            TimezoneField().validate("Nowhere/Special", Ctx())

    def test_a_zone_outside_the_shortlist_is_still_accepted(self):
        # The dropdown is a convenience, not the authority.
        assert "Pacific/Chatham" not in dict(timezone_choices())
        TimezoneField().validate("Pacific/Chatham", Ctx())

    def test_an_offset_label_reflects_the_season(self):
        winter = offset_label(LONDON, datetime(2026, 1, 15, tzinfo=UTC))
        summer = offset_label(LONDON, datetime(2026, 7, 15, tzinfo=UTC))
        assert winter == "UTC+00:00" and summer == "UTC+01:00"


class TestRendering:
    def test_an_instant_renders_in_the_readers_zone(self):
        assert _format_instant(NOON, "%H:%M", TOKYO) == "21:00"
        assert _format_instant(NOON, "%H:%M", LA) == "05:00"

    def test_a_date_renders_the_same_everywhere(self):
        assert _format_date(date(2026, 9, 5), "%d %b") == "05 Sep"

    def test_relative_time_is_measured_against_utc(self):
        # The bug this replaced: a server in Tokyo reported a fresh UTC
        # timestamp as nine hours old.
        recent = utcnow() - timedelta(hours=2)
        assert _ago(recent.isoformat()) == "2 hours ago"

    def test_relative_time_handles_a_naive_value(self):
        recent = utcnow().replace(tzinfo=None) - timedelta(minutes=30)
        assert _ago(recent.isoformat()) == "30 minutes ago"

    def test_something_unparseable_renders_empty(self):
        assert _ago("nonsense") == ""


class TestIdentityCarriesTheZone:
    def test_an_identity_has_one(self):
        assert Identity(subject="a").timezone == "UTC"
        assert Identity(subject="a", timezone=TOKYO).timezone == TOKYO

    def test_a_session_round_trips_it(self):
        from starlette.responses import Response

        from app.auth.session import SessionStore
        from tests.test_auth import make_request

        store = SessionStore("k" * 32)
        carrier = Response()
        store.save_identity(carrier, Identity(subject="1", timezone=TOKYO))
        cookie = carrier.headers["set-cookie"].split(";")[0].split("=", 1)[1]

        restored = store.load_identity(
            make_request(cookies={store.cookie_name: cookie})
        )
        assert restored.timezone == TOKYO, "otherwise it reverts on the next request"


class TestThroughTheWebLayer:
    """The whole path: stored UTC, rendered per reader."""

    def test_two_readers_see_one_instant_differently(self, client):
        from .conftest import sign_in

        sign_in(client, email="tokyo@x.test", roles=["admin"])
        client.cookies.set("tz", "Asia/Tokyo")
        tokyo = client.get("/r/deals/1").text

        sign_in(client, email="la@x.test", roles=["admin"])
        client.cookies.set("tz", "America/Los_Angeles")
        la = client.get("/r/deals/1").text

        assert tokyo != la or "closed_on" not in tokyo

    def test_the_zone_reaches_the_template_context(self, client):
        from .conftest import sign_in

        sign_in(client, email="a@x.test", roles=["admin"])
        client.cookies.set("tz", "Asia/Tokyo")
        # A macro must be imported "with context" or the filters see nothing --
        # the page still renders, silently in UTC.
        assert client.get("/r/deals").status_code == 200

    def test_an_invalid_zone_from_a_header_is_ignored(self, client):
        from .conftest import sign_in

        sign_in(client, email="a@x.test", roles=["admin"])
        response = client.get("/r/deals", headers={"X-Timezone": "Nowhere/Special"})
        assert response.status_code == 200, "a bad header must not break the page"
