"""Time.

One rule, applied everywhere: **instants are stored in UTC and displayed in the
viewer's timezone**. Nothing in this application calls ``datetime.now()``,
because that returns the server's local wall clock -- a value that changes
meaning if the server moves, differs between two workers in different regions,
and is ambiguous for one hour every autumn.

The distinction that matters most here is between an *instant* and a *date*:

* An instant -- when a record was created, when a message was sent -- is a
  point on the world's timeline. It is stored in UTC and rendered in whatever
  zone the reader is in.
* A date -- a birthday, an invoice date, the day a deal is expected to close --
  is a calendar entry, not a moment. Converting it between zones is wrong: a
  deal closing on the 5th does not close on the 4th because the reader is in
  Los Angeles.

So ``DateField`` values are never converted, and ``DateTimeField`` values
always are.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: The zone everything is stored in.
UTC_ZONE = UTC

#: Fallback when a caller's zone is unknown or unreadable.
DEFAULT_TIMEZONE = "UTC"


def utcnow() -> datetime:
    """The current instant, timezone-aware, in UTC.

    Use this instead of ``datetime.now()`` -- always.
    """
    return datetime.now(UTC)


def today(tz: str | tzinfo | None = None) -> date:
    """Today's date *in a given zone*.

    Not simply ``date.today()``: at 23:00 in London it is already tomorrow in
    Tokyo, so "today" depends on who is asking. A "due today" filter that used
    the server's date would be wrong for half the world.
    """
    return utcnow().astimezone(resolve(tz)).date()


def resolve(tz: str | tzinfo | None) -> tzinfo:
    """A timezone object from a name, tolerating nonsense.

    An unknown or misspelled zone falls back to UTC rather than raising: a bad
    value in one user's preferences should not break their page.
    """
    if tz is None:
        return UTC
    if isinstance(tz, tzinfo):
        return tz
    name = str(tz).strip()
    if not name or name.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return UTC


def is_valid_timezone(name: str) -> bool:
    """Whether ``name`` is a zone this system knows."""
    if not name or name.upper() == "UTC":
        return True
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False
    return True


def to_utc(value: datetime, assume: str | tzinfo | None = None) -> datetime:
    """Convert an instant to UTC for storage.

    A naive value is *assumed* to be in ``assume`` -- the zone of whoever
    submitted it. That assumption is the only sensible one: a user typing
    "14:00" into a form means two o'clock where they are.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=resolve(assume)).astimezone(UTC)
    return value.astimezone(UTC)


def to_zone(value: datetime, tz: str | tzinfo | None) -> datetime:
    """Convert a stored instant into the reader's zone for display.

    A naive value is treated as UTC, which is what this application stores.
    Older rows written before timezones were handled are therefore read
    correctly rather than shifted by the server's offset.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(resolve(tz))


def parse(value: object, *, assume: str | tzinfo | None = None) -> datetime | None:
    """Read an instant from whatever a driver or a form handed over.

    Returns ``None`` rather than raising, because a column holding something
    unparseable should render blank, not break the page around it.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=resolve(assume))
    if isinstance(value, date):
        # A bare date at midnight in the assumed zone.
        return datetime.combine(value, time.min, tzinfo=resolve(assume))
    if not value:
        return None

    text = str(value).strip().replace(" ", "T")
    # Python understands "+00:00" but not the "Z" that JSON and many APIs use.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=resolve(assume))


def parse_date(value: object) -> date | None:
    """Read a calendar date. Never converted between zones."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def day_bounds(day: date, tz: str | tzinfo | None) -> tuple[datetime, datetime]:
    """The UTC instants a calendar day begins and ends at, in a given zone.

    What a "today" filter needs: the 3rd in Tokyo is a different span of
    universal time from the 3rd in Los Angeles, and comparing a stored UTC
    column against a bare date would silently use the server's idea of the day.
    """
    zone = resolve(tz)
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = start + timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def offset_label(tz: str | tzinfo | None, at: datetime | None = None) -> str:
    """A zone's current offset, as ``UTC+01:00`` -- for showing in a form.

    Computed at a moment rather than fixed, because an offset is not a property
    of a zone: London is UTC+00:00 in January and UTC+01:00 in July.
    """
    moment = (at or utcnow()).astimezone(resolve(tz))
    offset = moment.utcoffset()
    if offset is None:
        return "UTC"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    hours, minutes = divmod(abs(total) // 60, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def common_timezones() -> tuple[str, ...]:
    """A short list for a preferences dropdown.

    Deliberately not the full IANA database: several hundred entries in a
    select is worse than a curated few dozen, and anything missing can still be
    typed in.
    """
    return (
        "UTC",
        "Europe/London", "Europe/Dublin", "Europe/Lisbon", "Europe/Paris",
        "Europe/Berlin", "Europe/Madrid", "Europe/Rome", "Europe/Amsterdam",
        "Europe/Stockholm", "Europe/Warsaw", "Europe/Athens", "Europe/Istanbul",
        "Europe/Moscow",
        "Africa/Lagos", "Africa/Cairo", "Africa/Johannesburg", "Africa/Nairobi",
        "Asia/Dubai", "Asia/Karachi", "Asia/Kolkata", "Asia/Dhaka",
        "Asia/Bangkok", "Asia/Singapore", "Asia/Hong_Kong", "Asia/Shanghai",
        "Asia/Tokyo", "Asia/Seoul", "Asia/Jerusalem",
        "Australia/Perth", "Australia/Adelaide", "Australia/Sydney",
        "Pacific/Auckland",
        "America/Sao_Paulo", "America/Argentina/Buenos_Aires", "America/Bogota",
        "America/Mexico_City", "America/New_York", "America/Toronto",
        "America/Chicago", "America/Denver", "America/Phoenix",
        "America/Los_Angeles", "America/Vancouver", "America/Anchorage",
        "Pacific/Honolulu",
    )


def timezone_choices() -> list[tuple[str, str]]:
    """``(value, label)`` pairs with the current offset shown.

    Seeing "Europe/London (UTC+01:00)" is how a person confirms they picked the
    right one.
    """
    return [(name, f"{name.replace('_', ' ')} ({offset_label(name)})")
            for name in common_timezones()]
