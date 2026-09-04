# Several databases

A resource names the connection it lives on, so putting two resources in two
databases has always been a one-word change:

```python
Resource("deals",     provider="db.main#deals",       ...)
Resource("audit_2019", provider="db.archive#deals_2019", ...)
```

This page covers the three things that get harder once you do: showing rows
from both in one screen, migrating them, and seeding them.

## One resource, several backends

`Union` declares that a resource's rows live in more than one place. The views
do not change, because a union is a provider and every view is written against
providers.

```python
from app.providers.union import Union, UnionSource, source_choices

DEAL_STORES = Union(
    UnionSource("db.main#deals",             label="live"),
    UnionSource("db.archive#archived_deals", label="archive"),
    source_field="store",
)

Resource(
    "all_deals",
    provider=DEAL_STORES,
    policy=ReadOnlyPolicy(),
    fields=[
        TextField("name", searchable=True),
        SelectField("store", choices=source_choices(DEAL_STORES), in_filter=True),
        CurrencyField("amount"),
        StatusField("stage", choices=STAGES, in_filter=True),
    ],
)
```

That is the whole declaration. `modules/demo_archive` is the working version,
and it runs with no services beyond a second SQLite file:

```bash
CRM_ARCHIVE_ENABLED=true CRM_MODULES=demo_crm,demo_sales,demo_archive uv run crm migrate --all
CRM_ARCHIVE_ENABLED=true CRM_MODULES=demo_crm,demo_sales,demo_archive uv run crm seed
CRM_ARCHIVE_ENABLED=true CRM_MODULES=demo_crm,demo_sales,demo_archive uv run crm seed -c db.archive
CRM_ARCHIVE_ENABLED=true CRM_MODULES=demo_crm,demo_sales,demo_archive uv run crm dev
```

### What it costs

Serving page N of a merged, sorted list needs `offset + page_size` rows from
**each** source, merged and then sliced — asking each for one page and
interleaving would skip rows. So a union's cost is bounded by how deep you page
and by the number of sources, never by how much data they hold. Past
`max_rows` (5000 by default) it refuses rather than truncating, exactly as the
capability shim does.

Measured on the demo, two SQLite databases:

| Page | Queries | Where the work happens |
| --- | --- | --- |
| list, both stores | 4 | rows + count, once per database |
| list, filtered to one store | 2 | the other database is never opened |
| chart or pivot | 2 | each database groups its own rows; only the groups are merged |

### The source column

Every record is stamped with the source it came from, under `source_field`
(`"source"` unless you rename it — the demo uses `"store"` because `deals`
already has a column called `source`). Declare a field of that name and it
behaves like any other: displayable, filterable, groupable.

Filtering on it is the cheapest filter a union has. `store = "archive"` is not
pushed into the backends — the column does not exist in either — it decides
**which backends to open**, so filtering to one store does not query the other
at all. Only `=`, `!=`, `in` and `not in` can be answered that way, and only in
a top-level `AND`; `source = 'a' OR amount > 10` is refused rather than
answered approximately, because it would need rows from a source the condition
excludes.

### What a union will not do

**Write.** A row belongs to one of several databases and a form has no way to
say which, so create, update and delete are refused with a message rather than
guessed at. Declare a resource per source for editing, or give the union
resource a `write_provider`.

**Average across sources.** The mean of two means is not the mean, and
weighting it correctly needs a count of non-null values the query language
cannot ask for. Counts, sums, minima and maxima all merge from partial results
and stay in the databases; an average falls back to merging rows, under the
same row budget.

**Hide a failure.** If a source is unreachable the query fails. A union that
silently dropped it would return a short answer that looks complete, which is
the same failure the shim refuses to make when a filter cannot be pushed down.

**Guarantee unique keys.** Two databases will both have an `id` of 5. Pass
`prefixed=True` and every key becomes `<label>:<key>`, so a detail route
addresses one row; leave it off and sources are searched in declaration order,
which is right when the keys are UUIDs or the ranges are known not to overlap.

## Migrating

Each database has its own migration history, in its own directory, tracked in
its own version table:

| Connection | History | Version table |
| --- | --- | --- |
| `db.main` | `migrations/versions/` | `alembic_version` |
| `db.archive` | `migrations/versions_db_archive/` | `alembic_version_db_archive` |

Both halves matter, and they fail differently. A shared history would run one
database's migrations against another; a shared version table would leave two
databases at two revisions each believing they were at the other's. The default
connection keeps alembic's own layout, so a single-database deployment is
unaffected by any of this.

```bash
uv run crm make-migration --connection db.archive "archive tables"
uv run crm migrate --connection db.archive
uv run crm migrate --all          # every database, platform first
```

Autogenerate compares only the tables belonging to `--connection`, so a
migration for one database never proposes creating another's.

### Where the mapping comes from

Nothing declares it. `app.core.placement` reads it back out of the resource
declarations — `provider="db.archive#archived_deals"` says where
`archived_deals` lives — so the schema and its placement cannot drift apart.
Tables nothing claims belong to the default connection, which is why half the
schema (reset tokens, timeline entries) needs no annotation and a
single-database deployment needs none at all.

### Foreign keys do not cross

A constraint between two databases is not a constraint: it cannot be created,
and if it is, it points at a table that is not there. Design around it — the
demo's `archived_deals` keeps `company_name` rather than a `company_id` — and
resolve the reference in the application with a `RelationField`, which is
already how relations work across backends.

## Seeding

`crm seed` creates and drops the tables belonging to one connection, so seeding
one database cannot delete another's:

```bash
uv run crm seed                    # the platform database
uv run crm seed -c db.archive      # its own tables, its own sample data
```

The accounts live wherever the `users` table does. Seeding a database that does
not hold them creates its tables and stops, which is the right answer rather
than a limitation: there is one set of accounts, not one per database. A
module whose tables are on another connection is skipped rather than allowed to
fail halfway through inserting.

## Resources that are not in a database at all

A resource may name a connection that holds no tables — an HTTP endpoint, a
queue, a Redis keyspace. That is ordinary, and it interacts with the placement
rule above in one way worth knowing.

"Tables nothing claims belong to the default connection" is what makes a
single-database deployment need no configuration. Left alone it would also
adopt the name of a resource that is deliberately *not* in SQL, and create a
permanently empty table for it. So a name spoken for by a non-table connection
is recorded as belonging nowhere, and no database creates it.

One consequence: **moving a resource off SQL makes its old table dead weight,
and the next autogenerated migration will propose dropping it.** That is
correct — the schema genuinely no longer includes it — but it is a destructive
operation appearing in a migration you did not ask for, so read what
`crm make-migration` produces, as ever. An existing table is not dropped until
you run such a migration; switching `CRM_JOBS_CONNECTION` to Redis leaves the
SQL `jobs` table sitting there, empty and harmless, until you say otherwise.

## Operational notes

- **Connections open lazily and independently.** A dead archive does not stop
  the live screens from serving — until something reads the union, which will
  then fail rather than quietly return half the rows.
- **`crm check-connections`** opens every configured database and reports each
  one, which is the fastest way to find out that only one of the two is
  reachable.
- **A disabled connection is never opened**, so `enabled: false` is how a
  second database stays configured and costs nothing.
- **Row scoping applies per source.** The identity's scope filter is pushed
  into each backend separately; a union cannot be used to see rows a policy
  would have hidden.
