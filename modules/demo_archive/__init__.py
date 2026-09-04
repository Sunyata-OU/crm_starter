"""Demo: one resource reading from two databases.

Closed deals are moved to an archive database and out of the operational one.
That is an ordinary thing to do and it usually costs you the screens: the live
list no longer shows history, and the archive gets a second, worse UI or none
at all.

Here it costs nothing. ``all_deals`` names both stores and gets the same list,
filters, search, sorting, pagination, chart and pivot as any other resource --
because a union is a provider, and every view is written against providers.

    provider=Union(
        UnionSource("db.main#deals", label="live"),
        UnionSource("db.archive#archived_deals", label="archive"),
    )

Optional and off by default. Unlike ``demo_remote`` it needs no services: the
archive is a second SQLite file, so this runs anywhere the rest of the demo
does.

    CRM_ARCHIVE_ENABLED=true \\
    CRM_MODULES=demo_crm,demo_sales,demo_archive uv run crm seed
    CRM_ARCHIVE_ENABLED=true \\
    CRM_MODULES=demo_crm,demo_sales,demo_archive uv run crm seed -c db.archive
    CRM_ARCHIVE_ENABLED=true \\
    CRM_MODULES=demo_crm,demo_sales,demo_archive uv run crm dev

Two seed runs, one per database, because each holds different tables. The same
is true of ``crm migrate``, which is what ``crm migrate --all`` is for.

Three details in the declaration below are worth the read: why the source
column is called ``store`` and not ``source``, why the resource is read-only,
and why the archive's ``origin`` column is renamed rather than mapped.
"""

from __future__ import annotations

from app.core.query import Agg, Condition, Measure, Op
from app.core.registry import Registry
from app.fields.types import (
    CurrencyField,
    DateField,
    SelectField,
    StatusField,
    TextAreaField,
    TextField,
)
from app.providers.union import Union, UnionSource, source_choices
from app.resources.policy import ReadOnlyPolicy
from app.resources.resource import Resource
from app.resources.views import (
    ChartView,
    Column,
    DetailView,
    ListView,
    PivotView,
    QuickFilter,
    SearchSpec,
    Section,
)

MANIFEST = {
    "name": "demo_archive",
    "label": "Demo: archived deals",
    "description": "One resource served from two databases at once.",
    "depends": ("demo_sales",),
    "menu_groups": {"Sales": 20},
    # Needs the db.archive connection enabled, so it is never loaded implicitly.
    "optional": True,
}

STAGES = [
    ("qualifying", "Qualifying", "default"),
    ("proposal", "Proposal", "blue"),
    ("negotiation", "Negotiation", "amber"),
    ("won", "Won", "green"),
    ("lost", "Lost", "red"),
]

#: Both halves of the union, named once so the resource, the seeder and the
#: source column's choices cannot disagree about what the stores are called.
DEAL_STORES = Union(
    UnionSource("db.main#deals", label="live"),
    UnionSource("db.archive#archived_deals", label="archive"),
    # Not "source": ``deals`` already has a column by that name holding how the
    # lead arrived, and stamping over it would replace real data with the name
    # of a database. The clash is worth noticing rather than working around --
    # any column name the union adds has to be one no source already uses.
    source_field="store",
)


def register(registry: Registry) -> None:
    registry.add_resource(_all_deals())


def _all_deals() -> Resource:
    """Every deal, live and archived, as one set of screens.

    Read-only on purpose, and not because the union refuses writes -- though it
    does. A row here belongs to one of two databases and the form has no way to
    say which, so the honest edit path is the ``deals`` resource for live rows
    and a restore for archived ones. Offering a save that would have to guess
    is the same mistake as offering inline editing over a read-only API.
    """
    return Resource(
        "all_deals",
        provider=DEAL_STORES,
        label="Deal (all)",
        label_plural="All deals",
        icon="⛁",
        menu_group="Sales",
        display_field="name",
        default_sort=["-closed_on"],
        policy=ReadOnlyPolicy(),
        fields=[
            TextField("id", in_form=False),
            TextField("name", label="Deal", searchable=True),
            # Declared like any other field. The union stamps it onto every
            # record, and a filter on it picks which databases to visit rather
            # than being pushed into them -- so filtering to "archive" does not
            # query the live database at all.
            SelectField(
                "store", label="Stored in", choices=source_choices(DEAL_STORES), in_filter=True
            ),
            CurrencyField("amount", in_filter=True),
            StatusField("stage", choices=STAGES, in_filter=True),
            TextField("owner", in_filter=True, searchable=True),
            DateField("expected_close", label="Expected"),
            DateField("closed_on", label="Closed"),
            TextAreaField("notes"),
        ],
        search=SearchSpec(
            fields=("name", "owner"),
            filters=("store", "stage", "owner"),
            group_by=("store", "stage", "owner"),
            quick_filters=[
                # A chip on the source column, which is the cheapest filter the
                # union has: it decides which databases to open rather than
                # what to select from them.
                QuickFilter(
                    "archived", "Archived", icon="⛁",
                    description="Deals that have been moved out of the live database.",
                    build=lambda identity: Condition("store", Op.EQ, "archive"),
                ),
                QuickFilter(
                    "live", "Live", icon="◉",
                    description="Deals still in the operational database.",
                    build=lambda identity: Condition("store", Op.EQ, "live"),
                ),
                QuickFilter(
                    "won", "Won", icon="◆",
                    description="Closed and won, wherever they are stored.",
                    build=lambda identity: Condition("stage", Op.EQ, "won"),
                ),
            ],
        ),
        views=[
            ListView(
                columns=[
                    Column("name", link=True, width="34%"),
                    "store", "stage", "amount", "owner", "closed_on",
                ],
                default_sort=["-closed_on"],
                # Nothing here can be saved, so nothing here offers to be.
                inline_edit=False,
            ),
            DetailView(
                sections=[
                    Section("Deal", ["name", "stage", "amount", "owner"]),
                    Section("Dates", ["expected_close", "closed_on"]),
                    Section("Origin", ["store", "notes"]),
                ]
            ),
            # Charts and pivots work unchanged: the union aggregates in each
            # database and merges the groups, so the counts and sums below are
            # computed by two databases rather than by this process.
            ChartView(
                group_by="stage",
                measure=Measure(Agg.SUM, "amount", alias="value"),
                label="Value by stage",
                chart="column",
            ),
            PivotView(
                rows=("store",),
                columns=("stage",),
                measures=(Measure(Agg.COUNT, alias="deals"),),
                label="Store × stage",
            ),
        ],
    )
