"""View specifications.

A view says *what* to show, never *how* to fetch or draw it. The generic routes
read these specs to build a query; the templates read them to lay out the page.
Because they are ordinary Python objects, a later module can adjust a view
another module declared -- adding a column, hiding a section -- without editing
its source or duplicating the whole declaration.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Literal, Self

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


class CalendarView(View):
    """Records placed on a month or week grid by a date field."""

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
        default_scale: Literal["month", "week"] = "month",
    ) -> None:
        super().__init__(name=name, label=label, icon=icon)
        self.start_field = start_field
        self.end_field = end_field
        self.title_field = title_field
        self.color_field = color_field
        self.all_day = all_day
        self.default_scale = default_scale

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
        limit: int = 25,
    ) -> None:
        super().__init__(name=name, label=label, icon=icon)
        self.group_by = group_by
        self.measure = measure or Measure(Agg.COUNT, alias="value")
        self.chart = chart
        #: Optional second grouping, drawn as separate series.
        self.series_by = series_by
        self.sort_by_value = sort_by_value
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
        group_by: Sequence[str] = (),
        placeholder: str = "Search",
    ) -> None:
        #: Columns free-text search looks in. Empty means "every searchable field".
        self.fields = tuple(fields)
        #: Columns offered in the filter builder. Empty means "every filterable field".
        self.filters = tuple(filters)
        self.quick_filters = list(quick_filters)
        self.group_by = tuple(group_by)
        self.placeholder = placeholder
