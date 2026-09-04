"""Templates, and how one is chosen.

The override chain is the feature that makes this a starter rather than a
framework you fight. Asking for a resource's list view looks first for a
template written specifically for that resource and falls back to the generic
one, so customising a single screen never means copying the machinery behind it:

    templates/resources/contacts/list.html   <- yours, if it exists
    templates/views/list.html                <- the generic one

The same applies to individual fields, so a project can restyle how one column
renders without touching the table macro.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from jinja2 import (
    ChainableUndefined,
    ChoiceLoader,
    Environment,
    FileSystemLoader,
    TemplateNotFound,
    pass_context,
    select_autoescape,
)
from markupsafe import Markup, escape
from starlette.requests import Request
from starlette.responses import HTMLResponse

from app.fields.base import EMPTY, Field
from app.settings import Settings


class Templates:
    """A configured Jinja environment plus the resolution helpers."""

    def __init__(self, settings: Settings, extra_dirs: tuple[Path, ...] = ()) -> None:
        self.settings = settings
        loaders = [FileSystemLoader(str(d)) for d in extra_dirs]
        loaders.append(FileSystemLoader(str(settings.templates_dir)))
        self.env = Environment(
            loader=ChoiceLoader(loaders),
            autoescape=select_autoescape(["html", "xml"]),
            auto_reload=settings.template_reload,
            trim_blocks=True,
            lstrip_blocks=True,
            # A template asking for a field a particular record did not return
            # is normal with heterogeneous backends. Render a blank cell rather
            # than raising, and allow chained access on the missing value.
            undefined=ChainableUndefined,
        )
        self._install_filters()
        self._template_cache: dict[tuple[str, str], str] = {}

    # -- resolution ---------------------------------------------------------

    def view_template(self, resource_name: str, view_kind: str) -> str:
        """The most specific template available for this resource and view."""
        return self._resolve(
            (resource_name, f"view:{view_kind}"),
            [f"resources/{resource_name}/{view_kind}.html", f"views/{view_kind}.html"],
        )

    def field_template(self, resource_name: str, field: Field, mode: str) -> str:
        """The partial that renders one field, in ``display`` or ``input`` mode.

        Three levels of specificity: this field on this resource, this field
        type anywhere, then a plain text fallback so an unstyled custom type
        still renders something.
        """
        filename = field.display_template if mode == "display" else field.input_template
        widget = f"{field.widget}.html" if field.widget else filename
        return self._resolve(
            (resource_name, f"field:{mode}:{field.name}:{widget}"),
            [
                f"resources/{resource_name}/fields/{mode}/{field.name}.html",
                f"fields/{mode}/{widget}",
                f"fields/{mode}/{filename}",
                f"fields/{mode}/text.html",
            ],
        )

    def _resolve(self, key: tuple[str, str], candidates: list[str]) -> str:
        """First candidate that exists, memoised.

        Caching matters: without it every rendered cell would stat the
        filesystem several times. Disabled while template reload is on so
        adding an override during development takes effect immediately.
        """
        if not self.settings.template_reload and key in self._template_cache:
            return self._template_cache[key]
        for candidate in candidates:
            try:
                self.env.get_template(candidate)
            except TemplateNotFound:
                continue
            self._template_cache[key] = candidate
            return candidate
        raise TemplateNotFound(candidates[-1])

    def exists(self, name: str) -> bool:
        try:
            self.env.get_template(name)
        except TemplateNotFound:
            return False
        return True

    # -- rendering ----------------------------------------------------------

    def render(self, name: str, context: Mapping[str, Any]) -> str:
        return self.env.get_template(name).render(dict(context))

    def response(
        self,
        name: str,
        context: Mapping[str, Any],
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> HTMLResponse:
        return HTMLResponse(
            self.render(name, context), status_code=status_code, headers=dict(headers or {})
        )

    # -- filters ------------------------------------------------------------

    def _install_filters(self) -> None:
        s = self.settings

        def money(value: Any, symbol: str = s.currency_symbol) -> str:
            return _money(value, symbol)

        @pass_context
        def as_date(ctx: Any, value: Any, fmt: str = s.date_format) -> str:
            """A calendar date, rendered as-is.

            Never converted: a deal closing on the 5th does not close on the
            4th because the reader is in Los Angeles.
            """
            return _format_date(value, fmt)

        @pass_context
        def as_datetime(ctx: Any, value: Any, fmt: str = s.datetime_format) -> str:
            """An instant, rendered in the reader's zone."""
            return _format_instant(value, fmt, ctx.get("timezone"))

        @pass_context
        def as_time(ctx: Any, value: Any, fmt: str = "%H:%M") -> str:
            return _format_instant(value, fmt, ctx.get("timezone"))

        @pass_context
        def ago(ctx: Any, value: Any) -> str:
            return _ago(value, ctx.get("timezone"))

        @pass_context
        def local_input(ctx: Any, value: Any) -> str:
            """A stored instant as a ``datetime-local`` input expects it.

            That control has no concept of a zone, so it must be handed the
            reader's wall-clock reading -- and what it returns is interpreted
            in the same zone.
            """
            from app.core.clock import parse as parse_instant
            from app.core.clock import to_zone

            parsed = parse_instant(value)
            if parsed is None:
                return ""
            return to_zone(parsed, ctx.get("timezone")).strftime("%Y-%m-%dT%H:%M")

        self.env.filters.update(
            {
                "money": money,
                "date": as_date,
                "datetime": as_datetime,
                "time": as_time,
                "local_input": local_input,
                "ago": ago,
                "yesno": _yesno,
                "blank": _blank,
                "initials": _initials,
                "truncate_chars": _truncate,
                "duration": _duration,
                "querystring": _querystring,
                "file_info": _file_info,
                "filesize": _filesize,
            }
        )
        self.env.globals.update(
            {
                "settings": s,
                "app_name": s.app_name,
                "EMPTY": EMPTY,
                "now": datetime.now,
            }
        )


# -- filter implementations ------------------------------------------------


def _blank(value: Any, placeholder: str = "—") -> Any:
    """An em dash for missing values, so empty cells read as intentional."""
    if value is None or value is EMPTY or value == "":
        return Markup(f'<span class="muted">{escape(placeholder)}</span>')
    return value


def _money(value: Any, symbol: str = "$") -> str:
    if value is None or value == "":
        return ""
    try:
        amount = Decimal(str(value))
    except (ValueError, ArithmeticError):
        return str(value)
    negative = amount < 0
    formatted = f"{abs(amount):,.2f}"
    return f"-{symbol}{formatted}" if negative else f"{symbol}{formatted}"


def _coerce_temporal(value: Any) -> datetime | date | None:
    if isinstance(value, (datetime, date)):
        return value
    if isinstance(value, str) and value:
        text = value.replace(" ", "T").replace("Z", "+00:00")
        for parse in (datetime.fromisoformat, date.fromisoformat):
            try:
                return parse(text)
            except ValueError:
                continue
    return None


def _format_date(value: Any, fmt: str) -> str:
    """Format a calendar date. No conversion -- see ``core.clock``."""
    parsed = _coerce_temporal(value)
    if parsed is None:
        return "" if value is None else str(value)
    return parsed.strftime(fmt)


def _format_instant(value: Any, fmt: str, tz: Any = None) -> str:
    """Format a stored instant in the reader's zone."""
    from app.core.clock import parse as parse_instant
    from app.core.clock import to_zone

    parsed = parse_instant(value)
    if parsed is None:
        # Not an instant -- a plain date reached a datetime filter. Render it
        # rather than blanking the cell.
        return _format_date(value, fmt)
    return to_zone(parsed, tz).strftime(fmt)


def _ago(value: Any, tz: Any = None) -> str:
    """A relative time, for timelines and 'last updated' columns.

    Compared against the current instant in UTC, not against the server's wall
    clock -- otherwise a server in Tokyo reports a fresh UTC timestamp as nine
    hours old.
    """
    from app.core.clock import parse as parse_instant
    from app.core.clock import utcnow

    parsed = parse_instant(value)
    if parsed is None:
        return ""
    reference = utcnow()
    seconds = (reference - parsed).total_seconds()
    future = seconds < 0
    seconds = abs(seconds)
    for limit, divisor, unit in (
        (60, 1, "second"),
        (3600, 60, "minute"),
        (86400, 3600, "hour"),
        (2592000, 86400, "day"),
        (31536000, 2592000, "month"),
    ):
        if seconds < limit:
            count = int(seconds // divisor) or 1
            label = f"{count} {unit}{'s' if count != 1 else ''}"
            return f"in {label}" if future else f"{label} ago"
    years = int(seconds // 31536000) or 1
    label = f"{years} year{'s' if years != 1 else ''}"
    return f"in {label}" if future else f"{label} ago"


def _yesno(value: Any, yes: str = "Yes", no: str = "No") -> str:
    return yes if value else no


def _initials(value: Any) -> str:
    text = str(value or "").replace(".", " ").replace("_", " ")
    parts = [p for p in text.split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _truncate(value: Any, length: int = 80, suffix: str = "…") -> str:
    text = str(value or "")
    return text if len(text) <= length else text[: length - len(suffix)].rstrip() + suffix


def _duration(minutes: Any) -> str:
    try:
        total = int(minutes)
    except (TypeError, ValueError):
        return ""
    hours, mins = divmod(abs(total), 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def _file_info(value: Any) -> dict[str, Any]:
    """The parts of a stored file column, for the file and image partials."""
    from app.web.uploads import describe

    return describe(value)


def _filesize(size: Any) -> str:
    """A byte count as something a person reads."""
    try:
        amount = float(size)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1024 or unit == "GB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} GB"


def _querystring(request: Request, **changes: Any) -> str:
    """The current query string with some parameters changed or removed.

    Lets a template build a sort link or a page link without reassembling every
    filter the user already applied -- passing ``None`` drops a parameter.
    """
    params = dict(request.query_params)
    for key, value in changes.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = str(value)
    from urllib.parse import urlencode

    encoded = urlencode(params)
    return f"?{encoded}" if encoded else ""
