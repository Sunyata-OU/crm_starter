"""View specifications.

A view says *what* to show, never *how* to fetch or draw it. The generic routes
read these specs to build a query; the templates read them to lay out the page.
Because they are ordinary Python objects, a later module can adjust a view
another module declared -- adding a column, hiding a section -- without editing
its source or duplicating the whole declaration.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Literal, Self
from urllib.parse import quote

from app.core.query import Agg, Measure, Sort


class View:
    """Base class for every view kind.

    ``kind`` selects the template under ``templates/views/`` and the query the
    route builds.
    """

    kind: str = "list"
    #: Other names this view answers to in a URL.
    aliases: tuple[str, ...] = ()

    def __init__(self, *, name: str = "", label: str = "", icon: str = "") -> None:
        #: Distinguishes several views of the same kind on one resource.
        self.name = name or self.kind
        self.label = label or self.name.replace("_", " ").title()
        self.icon = icon

    def extend(self, **changes: Any) -> Self:
        """Mutate this view in place, returning it for chaining.

        In-place rather than copying because view specs are shared: a module
        adjusting another's view must affect the instance already registered.
        """
        for key, value in changes.items():
            if not hasattr(self, key):
                raise AttributeError(f"{type(self).__name__} has no setting {key!r}")
            setattr(self, key, value)
        return self

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"


# -- list ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Column:
    """One column in a table view."""

    field: str
    label: str = ""
    width: str = ""
    align: str = ""
    sortable: bool | None = None
    #: Render as a link to the record's detail page.
    link: bool = False
    css_class: str = ""

    @classmethod
    def coerce(cls, item: Column | str) -> Column:
        return item if isinstance(item, Column) else cls(field=item)


class ListView(View):
    """A paginated table."""

    kind = "list"

    def __init__(
        self,
        columns: Sequence[Column | str] = (),
        *,
        name: str = "",
        label: str = "",
        icon: str = "table",
        default_sort: Sequence[Sort | str] = (),
        page_size: int = 25,
        row_actions: Sequence[str] = (),
        bulk_actions: Sequence[str] = (),
        inline_edit: bool = True,
        row_link: bool = True,
        group_by: str = "",
        empty_message: str = "Nothing here yet.",
    ) -> None:
        super().__init__(name=name, label=label, icon=icon)
        self.columns = [Column.coerce(c) for c in columns]
        self.default_sort = tuple(
            s if isinstance(s, Sort) else Sort.parse(s) for s in default_sort
        )
        self.page_size = page_size
        self.row_actions = list(row_actions)
        self.bulk_actions = list(bulk_actions)
        self.inline_edit = inline_edit
        #: Whether clicking a row opens its detail page.
        self.row_link = row_link
        self.group_by = group_by
        self.empty_message = empty_message

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(c.field for c in self.columns)

    def add_columns(self, *columns: Column | str, after: str = "", before: str = "") -> Self:
        """Insert columns, optionally positioned relative to an existing one."""
        new = [Column.coerce(c) for c in columns]
        anchor = after or before
        if not anchor:
            self.columns.extend(new)
            return self
        index = next((i for i, c in enumerate(self.columns) if c.field == anchor), None)
        if index is None:
            self.columns.extend(new)
        else:
            at = index + 1 if after else index
            self.columns[at:at] = new
        return self

    def remove_columns(self, *field_names: str) -> Self:
        drop = set(field_names)
        self.columns = [c for c in self.columns if c.field not in drop]
        return self

    def reorder(self, *field_names: str) -> Self:
        """Put the named columns first, in the order given."""
        order = {name: i for i, name in enumerate(field_names)}
        self.columns.sort(key=lambda c: order.get(c.field, len(order)))
        return self


# -- form and detail -------------------------------------------------------


@dataclass(slots=True)
class Section:
    """A titled group of fields laid out in a grid."""

    title: str = ""
    fields: list[str] = dc_field(default_factory=list)
    columns: int = 2
    description: str = ""
    #: Render collapsed initially.
    collapsed: bool = False
    css_class: str = ""

    def __post_init__(self) -> None:
        self.fields = list(self.fields)


@dataclass(slots=True)
class Tab:
    """A named panel in a tabbed layout."""

    title: str
    sections: list[Section] = dc_field(default_factory=list)
    icon: str = ""

    def __post_init__(self) -> None:
        self.sections = list(self.sections)


class FormView(View):
    """The create and edit layout."""

    kind = "form"

    def __init__(
        self,
        sections: Sequence[Section] = (),
        *,
        name: str = "",
        label: str = "",
        icon: str = "edit",
        tabs: Sequence[Tab] = (),
        submit_label: str = "Save",
        #: Open in a modal from list views instead of navigating.
        modal: bool = True,
    ) -> None:
        super().__init__(name=name, label=label, icon=icon)
        self.sections = list(sections)
        self.tabs = list(tabs)
        self.submit_label = submit_label
        self.modal = modal

    @property
    def field_names(self) -> tuple[str, ...]:
        """Every field referenced, across sections and tabs, in layout order."""
        names: list[str] = []
        for section in self.all_sections:
            names.extend(f for f in section.fields if f not in names)
        return tuple(names)

    @property
    def all_sections(self) -> list[Section]:
        return [*self.sections, *(s for tab in self.tabs for s in tab.sections)]

    def section(self, title: str) -> Section:
        """The section with this title, raising if absent -- for extension code."""
        for s in self.all_sections:
            if s.title == title:
                return s
        raise KeyError(f"no section titled {title!r} in view {self.name!r}")

    def add_fields(self, *field_names: str, section: str = "") -> Self:
        """Append fields to a section, creating it if it does not exist."""
        if section:
            try:
                target = self.section(section)
            except KeyError:
                target = Section(title=section)
                self.sections.append(target)
        elif self.sections:
            target = self.sections[0]
        else:
            target = Section()
            self.sections.append(target)
        target.fields.extend(f for f in field_names if f not in target.fields)
        return self

    def remove_fields(self, *field_names: str) -> Self:
        drop = set(field_names)
        for s in self.all_sections:
            s.fields = [f for f in s.fields if f not in drop]
        return self


class DetailView(View):
    """The read-only record page: fields, related lists and a timeline."""

    kind = "detail"

    def __init__(
        self,
        sections: Sequence[Section] = (),
        *,
        name: str = "",
        label: str = "",
        icon: str = "file",
        tabs: Sequence[Tab] = (),
        title_field: str = "",
        subtitle_field: str = "",
        actions: Sequence[str] = (),
        #: Show the activity timeline alongside the record.
        timeline: bool = True,
    ) -> None:
        super().__init__(name=name, label=label, icon=icon)
        self.sections = list(sections)
        self.tabs = list(tabs)
        self.title_field = title_field
        self.subtitle_field = subtitle_field
        self.actions = list(actions)
        self.timeline = timeline

    @property
    def all_sections(self) -> list[Section]:
        return [*self.sections, *(s for tab in self.tabs for s in tab.sections)]

    @property
    def field_names(self) -> tuple[str, ...]:
        names: list[str] = []
        for section in self.all_sections:
            names.extend(f for f in section.fields if f not in names)
        return tuple(names)


# -- board -----------------------------------------------------------------


@dataclass(slots=True)
class Card:
    """What one card on a board shows."""

    title: str
    subtitle: str = ""
    body: str = ""
    badges: list[str] = dc_field(default_factory=list)
    avatar: str = ""
    color: str = ""

    def __post_init__(self) -> None:
        self.badges = list(self.badges)

    @property
    def field_names(self) -> tuple[str, ...]:
        parts = [self.title, self.subtitle, self.body, self.avatar, self.color, *self.badges]
        return tuple(dict.fromkeys(p for p in parts if p))


class BoardView(View):
    """Records grouped into draggable columns by one field -- a kanban board.

    Dragging a card writes the new group value back through the provider, so a
    board is only offered when the resource is updatable.

    Both ``board`` and ``kanban`` address this view in a URL, since the second
    is what most people will look for.
    """

    kind = "board"
    #: Alternative names accepted in ``?view=``.
    aliases = ("kanban",)

    def __init__(
        self,
        *,
        group_by: str,
        card: Card,
        name: str = "",
        label: str = "Board",
        icon: str = "columns",
        default_sort: Sequence[Sort | str] = (),
        draggable: bool = True,
        column_limit: int = 50,
        #: Show a per-column total of this numeric field.
        sum_field: str = "",
    ) -> None:
        super().__init__(name=name, label=label, icon=icon)
        self.group_by = group_by
        self.card = card
        self.default_sort = tuple(
            s if isinstance(s, Sort) else Sort.parse(s) for s in default_sort
        )
        self.draggable = draggable
        self.column_limit = column_limit
        self.sum_field = sum_field

    @property
    def field_names(self) -> tuple[str, ...]:
        extra = (self.group_by, self.sum_field)
        return tuple(dict.fromkeys([*self.card.field_names, *(f for f in extra if f)]))


# -- calendar --------------------------------------------------------------


#: The windows a calendar can be shown through. A month is listed with the
#: fixed-width scales even though its own width depends on which month it is.
CALENDAR_SCALES = ("day", "week", "fortnight", "month")


class CalendarView(View):
    """Records placed on a day, week, fortnight or month grid by a date field.

    Every scale is the same seven-column grid seen through a different window,
    so a day is one cell and a fortnight is two rows of the month's table. The
    scale is a request parameter, not a property of the view: two people can
    read the same calendar at different widths, and ``default_scale`` only says
    which one they land on.
    """

    kind = "calendar"

    def __init__(
        self,
        *,
        start_field: str,
        title_field: str,
        name: str = "",
        label: str = "Calendar",
        icon: str = "calendar",
        end_field: str = "",
        color_field: str = "",
        all_day: bool = True,
        default_scale: Literal["day", "week", "fortnight", "month"] = "month",
    ) -> None:
        super().__init__(name=name, label=label, icon=icon)
        self.start_field = start_field
        self.end_field = end_field
        self.title_field = title_field
        self.color_field = color_field
        self.all_day = all_day
        self.default_scale = (
            default_scale if default_scale in CALENDAR_SCALES else "month"
        )

    @property
    def field_names(self) -> tuple[str, ...]:
        candidates = (self.start_field, self.end_field, self.title_field, self.color_field)
        return tuple(dict.fromkeys(f for f in candidates if f))


# -- charts ----------------------------------------------------------------


class ChartView(View):
    """An aggregate rendered as bars, a line or a donut."""

    kind = "chart"

    def __init__(
        self,
        *,
        group_by: str,
        measure: Measure | None = None,
        name: str = "",
        label: str = "Chart",
        icon: str = "chart",
        chart: Literal["bar", "column", "line", "donut"] = "column",
        series_by: str = "",
        sort_by_value: bool = True,
        #: Order the groups by their own value rather than by the measure.
        #: A chart over time needs this: "the biggest month first" is a
        #: ranking, and a series read left to right has to be chronological or
        #: it is not a series. Setting it turns `sort_by_value` off, because
        #: the two orderings cannot both win.
        sort: Sequence[Sort | str] = (),
        limit: int = 25,
    ) -> None:
        super().__init__(name=name, label=label, icon=icon)
        self.group_by = group_by
        self.measure = measure or Measure(Agg.COUNT, alias="value")
        self.chart = chart
        #: Optional second grouping, drawn as separate series.
        self.series_by = series_by
        self.sort = tuple(s if isinstance(s, Sort) else Sort.parse(s) for s in sort)
        self.sort_by_value = sort_by_value and not self.sort
        self.limit = limit

    @property
    def group_fields(self) -> tuple[str, ...]:
        return tuple(f for f in (self.group_by, self.series_by) if f)


class PivotView(View):
    """A cross-tabulation of two groupings with one or more measures."""

    kind = "pivot"

    def __init__(
        self,
        *,
        rows: Sequence[str],
        columns: Sequence[str] = (),
        measures: Sequence[Measure] = (),
        name: str = "",
        label: str = "Pivot",
        icon: str = "grid",
        show_totals: bool = True,
    ) -> None:
        super().__init__(name=name, label=label, icon=icon)
        self.rows = tuple(rows)
        self.columns = tuple(columns)
        self.measures = tuple(measures) or (Measure(Agg.COUNT, alias="count"),)
        self.show_totals = show_totals

    @property
    def group_fields(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([*self.rows, *self.columns]))


# -- search ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuickFilter:
    """A one-click filter shown as a chip above a list.

    ``build`` is a callable rather than a fixed filter so a chip can mean
    "due this week" or "mine" -- both of which depend on when it is clicked and
    who clicks it.
    """

    name: str
    label: str
    #: ``(identity) -> Filter | None``. Built per request.
    build: Any
    icon: str = ""
    description: str = ""

    def resolve(self, identity: Any) -> Any:
        """The filter this chip stands for, for this caller, right now."""
        return self.build(identity) if callable(self.build) else self.build


class SearchSpec:
    """What the search bar and filter panel offer for a resource."""

    def __init__(
        self,
        *,
        fields: Sequence[str] = (),
        filters: Sequence[str] = (),
        quick_filters: Sequence[QuickFilter] = (),
        value_aliases: Mapping[str, Any] | None = None,
        group_by: Sequence[str] = (),
        placeholder: str = "Search",
    ) -> None:
        #: Columns free-text search looks in. Empty means "every searchable field".
        self.fields = tuple(fields)
        #: Columns offered in the filter builder. Empty means "every filterable field".
        self.filters = tuple(filters)
        self.quick_filters = list(quick_filters)
        #: Named filter values, written ``@name`` in the query string and
        #: resolved per request: ``{"my_region": lambda identity: ...}``.
        #: Shadows a built-in of the same name, so a resource whose owner
        #: column holds something other than an email can redefine ``@me``.
        self.value_aliases = dict(value_aliases or {})
        self.group_by = tuple(group_by)
        self.placeholder = placeholder


# -- tree ------------------------------------------------------------------


class TreeView(View):
    """A list whose rows expand to reveal their children, recursively.

    Two shapes, one spec. Left alone, ``parent_field`` names this resource's
    own parent column and the tree is self-referential: accounts under
    accounts, categories under categories. Naming ``child_resource`` instead
    hangs a *different* resource off each row -- contacts under companies --
    with ``parent_field`` naming the column over there that points back here.

    Children are fetched a level at a time, when a row is opened, rather than
    walked eagerly on the way in. A deep hierarchy would otherwise cost a query
    per node for a page that shows the top level and nothing else.
    """

    kind = "tree"
    aliases = ("hierarchy",)

    def __init__(
        self,
        columns: Sequence[Column | str] = (),
        *,
        parent_field: str,
        name: str = "",
        label: str = "Tree",
        icon: str = "tree",
        #: Resource hung under each row. Empty means this one -- a self-join.
        child_resource: str = "",
        default_sort: Sequence[Sort | str] = (),
        page_size: int = 50,
        #: How far a branch may be opened. Guards against a cycle in the data,
        #: which no schema here can rule out.
        max_depth: int = 8,
        child_limit: int = 100,
        row_link: bool = True,
        empty_message: str = "Nothing here yet.",
    ) -> None:
        super().__init__(name=name, label=label, icon=icon)
        self.columns = [Column.coerce(c) for c in columns]
        self.parent_field = parent_field
        self.child_resource = child_resource
        self.default_sort = tuple(
            s if isinstance(s, Sort) else Sort.parse(s) for s in default_sort
        )
        self.page_size = page_size
        self.max_depth = max_depth
        self.child_limit = child_limit
        self.row_link = row_link
        self.empty_message = empty_message

    @property
    def self_referential(self) -> bool:
        return not self.child_resource

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([*(c.field for c in self.columns), self.parent_field]))


# -- gantt -----------------------------------------------------------------


class GanttView(View):
    """Records drawn as bars along a time axis.

    The window is a fixed number of columns wide and the bars are positioned as
    percentages of it, so the whole thing is server-rendered: no measuring the
    viewport, and the page prints and screenshots as what it is.

    ``end_field`` is required rather than optional. A bar needs two dates, and
    inventing the second one -- "a day long", "until today" -- would draw a
    schedule the data does not claim.
    """

    kind = "gantt"
    aliases = ("timeline_chart",)

    def __init__(
        self,
        *,
        start_field: str,
        end_field: str,
        title_field: str = "",
        name: str = "",
        label: str = "Gantt",
        icon: str = "gantt",
        #: Rows are gathered into bands by this field, as a board is by status.
        group_by: str = "",
        color_field: str = "",
        #: Numeric 0-100 field drawn as a fill inside each bar.
        progress_field: str = "",
        default_scale: Literal["day", "week", "month"] = "week",
        #: Columns drawn across the window, in units of the scale.
        span: int = 12,
        limit: int = 200,
        default_sort: Sequence[Sort | str] = (),
    ) -> None:
        super().__init__(name=name, label=label, icon=icon)
        self.start_field = start_field
        self.end_field = end_field
        self.title_field = title_field
        self.group_by = group_by
        self.color_field = color_field
        self.progress_field = progress_field
        self.default_scale = default_scale
        self.span = span
        self.limit = limit
        self.default_sort = tuple(
            s if isinstance(s, Sort) else Sort.parse(s) for s in default_sort
        )

    @property
    def field_names(self) -> tuple[str, ...]:
        candidates = (
            self.start_field, self.end_field, self.title_field,
            self.group_by, self.color_field, self.progress_field,
        )
        return tuple(dict.fromkeys(f for f in candidates if f))

    @property
    def group_fields(self) -> tuple[str, ...]:
        """Fields whose choice labels the bands and bars are named by."""
        return tuple(f for f in (self.group_by, self.color_field) if f)


# -- map -------------------------------------------------------------------


class MapView(View):
    """Records as pins on a slippy map, placed by a latitude/longitude pair.

    Only records with both coordinates are asked for; the rest are counted and
    reported rather than silently dropped, since "not on the map" and "not in
    the data" are different problems and only one of them is the user's.
    """

    kind = "map"

    def __init__(
        self,
        *,
        lat_field: str,
        lon_field: str,
        title_field: str = "",
        name: str = "",
        label: str = "Map",
        icon: str = "map",
        subtitle_field: str = "",
        #: Field whose choice colours decide the pin colours.
        color_field: str = "",
        zoom: int = 4,
        limit: int = 500,
    ) -> None:
        super().__init__(name=name, label=label, icon=icon)
        self.lat_field = lat_field
        self.lon_field = lon_field
        self.title_field = title_field
        self.subtitle_field = subtitle_field
        self.color_field = color_field
        self.zoom = zoom
        self.limit = limit

    @property
    def field_names(self) -> tuple[str, ...]:
        candidates = (
            self.lat_field, self.lon_field,
            self.title_field, self.subtitle_field, self.color_field,
        )
        return tuple(dict.fromkeys(f for f in candidates if f))

    @property
    def group_fields(self) -> tuple[str, ...]:
        return (self.color_field,) if self.color_field else ()


# -- activity --------------------------------------------------------------


class ActivityView(View):
    """Who owes what, by when: records crossed by owner and kind of work.

    A cell holds the records sharing a row and a column, coloured by the
    soonest one still outstanding -- overdue, due today, or later. The states
    are computed from ``due_field`` against the request's clock rather than
    stored, so nothing has to be swept nightly to stay honest.
    """

    kind = "activity"

    def __init__(
        self,
        *,
        row_field: str,
        activity_field: str,
        due_field: str,
        name: str = "",
        label: str = "Activity",
        icon: str = "activity",
        title_field: str = "",
        #: Records whose work is finished, and so are not owed by anyone.
        done_filter: Any = None,
        limit: int = 500,
        default_sort: Sequence[Sort | str] = (),
    ) -> None:
        super().__init__(name=name, label=label, icon=icon)
        self.row_field = row_field
        self.activity_field = activity_field
        self.due_field = due_field
        self.title_field = title_field
        self.done_filter = done_filter
        self.limit = limit
        self.default_sort = tuple(
            s if isinstance(s, Sort) else Sort.parse(s) for s in default_sort
        )

    @property
    def field_names(self) -> tuple[str, ...]:
        candidates = (self.row_field, self.activity_field, self.due_field, self.title_field)
        return tuple(dict.fromkeys(f for f in candidates if f))

    @property
    def group_fields(self) -> tuple[str, ...]:
        return (self.row_field, self.activity_field)


# -- dashboard -------------------------------------------------------------


@dataclass(slots=True)
class Panel:
    """One view embedded in a dashboard.

    ``resource`` empty means the dashboard's own resource, so a resource can
    lay out its own charts without repeating its name.
    """

    view: str
    resource: str = ""
    title: str = ""
    #: Grid columns this panel occupies.
    span: int = 1
    height: str = ""
    #: Extra query parameters, so a panel can be a *filtered* view -- one work
    #: queue rather than the whole table. Anything the list route reads works:
    #: ``quick``, a ``f.<field>`` filter, a sort.
    params: Mapping[str, str] = dc_field(default_factory=dict)

    def url(self, default_resource: str) -> str:
        extra = "".join(
            f"&{quote(str(key))}={quote(str(value))}"
            for key, value in self.params.items()
        )
        return f"/r/{self.resource or default_resource}?view={self.view}&panel=1{extra}"


class DashboardView(View):
    """Several views on one page, each loaded on its own.

    Panels are fetched by the browser rather than rendered inline, which is
    what keeps a dashboard from being as slow as its slowest panel: the page
    arrives immediately and each panel fills in when its query returns. It also
    means a panel is exactly the view it names -- the same handler, the same
    template, the same permission check -- rather than a second implementation
    of it that can drift.
    """

    kind = "dashboard"

    def __init__(
        self,
        panels: Sequence[Panel] = (),
        *,
        name: str = "",
        label: str = "Dashboard",
        icon: str = "dashboard",
        columns: int = 2,
    ) -> None:
        super().__init__(name=name, label=label, icon=icon)
        self.panels = list(panels)
        self.columns = columns

    def add_panels(self, *panels: Panel) -> Self:
        self.panels.extend(panels)
        return self
