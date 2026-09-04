"""The resource -- one declaration that drives every screen.

Declaring a resource means naming its provider, listing its fields, and
optionally describing how those fields are laid out. Everything else is
derived: a list view falls back to the fields marked ``in_list``, a form view to
the fields marked ``in_form``, search to the fields marked ``searchable``.

The default views are generated lazily, so a resource with no explicit views is
fully usable and a resource with one explicit view still gets sensible defaults
for the rest.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from app.core.errors import RegistryError
from app.core.query import Condition, Filter, ListQuery, Op, Sort, and_
from app.core.results import Identity, Record
from app.fields.base import Field
from app.fields.types import BackrefField, RelationField
from app.resources.actions import Action
from app.resources.policy import Policy
from app.resources.views import (
    Column,
    DetailView,
    FormView,
    ListView,
    SearchSpec,
    Section,
    View,
)


class Resource:
    """A named collection of records, its fields, views, actions and policy."""

    def __init__(
        self,
        name: str,
        *,
        #: Reference into the connection registry, e.g. ``"db.main#contacts"``.
        provider: str | Any,
        fields: Sequence[Field],
        label: str = "",
        label_plural: str = "",
        icon: str = "box",
        pk: str = "id",
        #: Field whose value names a record in links, titles and typeaheads.
        display_field: str = "",
        views: Sequence[View] = (),
        actions: Sequence[Action] = (),
        policy: Policy | None = None,
        search: SearchSpec | None = None,
        default_sort: Sequence[Sort | str] = (),
        #: Show in the navigation. Lookup-only resources set this False.
        in_menu: bool = True,
        menu_group: str = "",
        menu_order: int = 100,
        description: str = "",
        #: Record activity against this resource on the timeline.
        timeline: bool = False,
        #: Write every change to the audit log. On by default: a resource that
        #: opts out should have to say so.
        audited: bool = True,
    ) -> None:
        if not fields:
            raise RegistryError(f"resource {name!r} declares no fields")
        self.name = name
        self.provider_ref = provider
        # Resource names are conventionally plural ("deals", "companies"), so
        # the plural label is the name humanised and the singular is derived
        # from it -- not the other way round.
        self.label_plural = label_plural or name.replace("_", " ").capitalize()
        self.label = label or _singularize(self.label_plural)
        self.icon = icon
        self.pk = pk
        self.fields = list(fields)
        self._field_map = {f.name: f for f in self.fields}
        if len(self._field_map) != len(self.fields):
            duplicates = _duplicates(f.name for f in self.fields)
            raise RegistryError(f"resource {name!r} declares duplicate fields: {duplicates}")
        self.display_field = display_field or self._guess_display_field()
        self._views: dict[str, View] = {v.name: v for v in views}
        self.actions = {a.name: a for a in actions}
        self.policy = policy or Policy()
        self.search = search or SearchSpec()
        self.default_sort = tuple(
            s if isinstance(s, Sort) else Sort.parse(s) for s in default_sort
        )
        self.in_menu = in_menu
        self.menu_group = menu_group
        self.menu_order = menu_order
        self.description = description
        self.timeline = timeline
        self.audited = audited
        #: Bound by the registry once connections are open.
        self.provider: Any = None
        #: Set when the resource is registered. Actions use it to reach other
        #: resources -- writing an activity when a deal closes, say -- without
        #: importing them, which would couple modules together.
        self.registry: Any = None

    # -- fields -------------------------------------------------------------

    def __iter__(self) -> Iterator[Field]:
        return iter(self.fields)

    def __contains__(self, name: object) -> bool:
        return name in self._field_map

    def field(self, name: str) -> Field:
        """The field called ``name``, or a clear error naming what exists."""
        try:
            return self._field_map[name]
        except KeyError:
            known = ", ".join(self._field_map) or "(none)"
            raise RegistryError(
                f"resource {self.name!r} has no field {name!r}; declared fields: {known}"
            ) from None

    def get_field(self, name: str) -> Field | None:
        return self._field_map.get(name)

    def add_field(self, field: Field, *, after: str = "") -> Resource:
        """Add a field, for a module extending a resource declared elsewhere."""
        if field.name in self._field_map:
            raise RegistryError(f"{self.name!r} already has a field {field.name!r}")
        self._field_map[field.name] = field
        if after and after in self._field_map:
            index = next(i for i, f in enumerate(self.fields) if f.name == after)
            self.fields.insert(index + 1, field)
        else:
            self.fields.append(field)
        return self

    def replace_field(self, field: Field) -> Resource:
        """Swap a field for a reconfigured version of it."""
        if field.name not in self._field_map:
            raise RegistryError(f"{self.name!r} has no field {field.name!r} to replace")
        self._field_map[field.name] = field
        self.fields = [field if f.name == field.name else f for f in self.fields]
        return self

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(self._field_map)

    @property
    def stored_fields(self) -> tuple[Field, ...]:
        """Fields that correspond to a column the provider stores.

        Excludes computed values and backrefs, which are resolved rather than
        read, and must never be sent to a provider as part of a write.
        """
        return tuple(
            f for f in self.fields
            if f.compute is None and not isinstance(f, BackrefField)
        )

    @property
    def relations(self) -> tuple[RelationField, ...]:
        return tuple(f for f in self.fields if isinstance(f, RelationField))

    @property
    def backrefs(self) -> tuple[BackrefField, ...]:
        return tuple(f for f in self.fields if isinstance(f, BackrefField))

    def searchable_fields(self) -> tuple[str, ...]:
        """Columns free-text search looks in.

        An explicit ``SearchSpec.fields`` wins; otherwise every field marked
        searchable; failing that, the display field, so search is never a no-op
        on a resource whose author did not think about it.
        """
        if self.search.fields:
            return self.search.fields
        marked = tuple(f.name for f in self.fields if f.searchable)
        if marked:
            return marked
        return (self.display_field,) if self.display_field else ()

    def filterable_fields(self) -> tuple[Field, ...]:
        if self.search.filters:
            return tuple(self.field(n) for n in self.search.filters)
        return tuple(f for f in self.fields if f.in_filter)

    def _guess_display_field(self) -> str:
        """Pick the field that best names a record."""
        for candidate in ("name", "title", "label", "subject", "email"):
            if candidate in self._field_map:
                return candidate
        # Fall back to the first text-ish field that is not the primary key.
        for f in self.fields:
            if f.name != self.pk and f.text_searchable:
                return f.name
        return self.pk

    def display_value(self, record: Mapping[str, Any]) -> str:
        """How this record is named in a link, title or typeahead."""
        value = record.get(self.display_field)
        if value in (None, ""):
            return f"{self.label} #{record.get(self.pk, '?')}"
        return str(value)

    # -- views --------------------------------------------------------------

    def view(self, name: str) -> View:
        """A view by name, generating a default if none was declared."""
        if name in self._views:
            return self._views[name]
        # "kanban" and "board" name the same thing; accept whichever the caller
        # reached for rather than sending them to a fallback list view.
        for candidate in self._views.values():
            if name in candidate.aliases:
                return candidate
        default = self._default_view(name)
        if default is None:
            known = ", ".join(sorted(self.view_names)) or "(none)"
            raise RegistryError(
                f"resource {self.name!r} has no view {name!r}; available views: {known}"
            )
        # Cache so extensions applied to a generated view survive.
        self._views[name] = default
        return default

    def has_view(self, name: str) -> bool:
        if name in self._views or self._default_view(name) is not None:
            return True
        return any(name in v.aliases for v in self._views.values())

    def add_view(self, view: View) -> Resource:
        self._views[view.name] = view
        return self

    @property
    def view_names(self) -> tuple[str, ...]:
        """Declared views plus the defaults that can always be generated."""
        return tuple(dict.fromkeys([*self._views, "list", "form", "detail"]))

    @property
    def declared_views(self) -> tuple[View, ...]:
        return tuple(self._views.values())

    def switchable_views(self) -> tuple[View, ...]:
        """Views offered in the list-page view switcher.

        Form and detail are excluded: they are reached through a record, not
        chosen as an alternative way of browsing.
        """
        return tuple(
            v for name, v in self._views.items()
            if v.kind not in ("form", "detail")
        ) or (self.view("list"),)

    def _default_view(self, name: str) -> View | None:
        """Build a view from the field flags when none was declared."""
        match name:
            case "list":
                return ListView(
                    columns=[
                        Column(f.name, link=(f.name == self.display_field), align=f.align)
                        for f in self.fields
                        if f.in_list
                    ],
                    default_sort=self.default_sort,
                )
            case "form":
                return FormView([
                    Section(fields=[f.name for f in self.fields if f.in_form and not f.readonly])
                ])
            case "detail":
                return DetailView(
                    sections=[Section(fields=[f.name for f in self.fields if f.in_detail])],
                    title_field=self.display_field,
                    timeline=self.timeline,
                )
        return None

    # -- queries ------------------------------------------------------------

    def build_query(
        self,
        identity: Identity,
        *,
        filter: Filter | None = None,
        search: str | None = None,
        sort: Sequence[Sort] = (),
        page: int = 1,
        page_size: int = 25,
        fields: Sequence[str] | None = None,
        with_total: bool = True,
    ) -> ListQuery:
        """Assemble a query with this caller's row scope already applied.

        Routes must go through here rather than constructing a ``ListQuery``
        directly; it is what guarantees the policy's scope is present.
        """
        return ListQuery(
            filter=filter,
            scope=self.policy.scope(identity),
            search=search or None,
            sort=tuple(sort) or self.default_sort,
            page=page,
            page_size=page_size,
            fields=tuple(fields) if fields else None,
            with_total=with_total,
        )

    def pk_filter(self, pk: Any) -> Filter:
        return Condition(self.pk, Op.EQ, pk)

    def scoped_pk_filter(self, pk: Any, identity: Identity) -> Filter | None:
        """A primary-key lookup that still honours the caller's row scope.

        Used for reads that must not leak a record the caller cannot list.
        """
        return and_(self.pk_filter(pk), self.policy.scope(identity))

    # -- actions ------------------------------------------------------------

    def action(self, name: str) -> Action:
        try:
            return self.actions[name]
        except KeyError:
            known = ", ".join(sorted(self.actions)) or "(none)"
            raise RegistryError(
                f"resource {self.name!r} has no action {name!r}; declared actions: {known}"
            ) from None

    def add_action(self, action: Action) -> Resource:
        self.actions[action.name] = action
        return self

    def actions_for(
        self, placement: str, record: Record | None, identity: Identity
    ) -> tuple[Action, ...]:
        """Actions to draw at ``placement``, filtered by role and availability."""
        return tuple(
            a for a in self.actions.values()
            if a.shown_in(placement)  # type: ignore[arg-type]
            and (a.visible_for(record, identity) if record is not None else a.allowed_for(identity))
        )

    # -- capability-aware permissions ---------------------------------------

    def can(self, op: str, identity: Identity, record: Record | None = None) -> bool:
        """Whether ``op`` is both permitted by policy and possible on the backend.

        Both halves matter: a policy may allow deletion while the provider is a
        read-only API, and offering the button either way would be a lie.
        """
        if not self.policy.allows(op, identity, record):  # type: ignore[arg-type]
            return False
        caps = getattr(self.provider, "capabilities", None)
        if caps is None:
            return True
        match op:
            case "create":
                return caps.create
            case "update":
                return caps.update
            case "delete":
                return caps.delete
        return caps.read

    def __repr__(self) -> str:
        return f"<Resource {self.name!r} ({len(self.fields)} fields)>"


def _singularize(label: str) -> str:
    """Derive a singular label from a plural resource name.

    Deliberately simple -- it covers the regular English cases and nothing more.
    Anything irregular ("people", "criteria") should pass ``label`` explicitly.
    """
    lowered = label.lower()
    if lowered.endswith("ies") and len(label) > 3:
        return label[:-3] + "y"
    if lowered.endswith(("sses", "xes", "zes", "ches", "shes")):
        return label[:-2]
    # "status", "analysis", "address" are not plurals despite the trailing s.
    if lowered.endswith("s") and not lowered.endswith(("ss", "us", "is")):
        return label[:-1]
    return label


def _duplicates(names) -> str:
    seen, dupes = set(), []
    for n in names:
        if n in seen:
            dupes.append(n)
        seen.add(n)
    return ", ".join(dupes)
