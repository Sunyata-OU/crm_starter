# Architecture

Three registries stacked on one another, and one adapter between two of them.

```
  Resources     what exists: fields, views, actions, policy
      │
      │  provider="db.main#contacts"
      ▼
  Providers     how to reach it: SQL, REST, queue, memory, composite
      │         ── CapabilityShim fills in whatever a backend cannot do ──
      │
      ▼
  Connections   named handles: engines, HTTP clients, broker channels
```

Nothing above the provider layer knows where data lives. Nothing below it knows
what a view is.

## The query language

Every read is a `ListQuery`. It carries a filter tree, a search term, sort keys,
a page — and, separately, a **scope**.

```python
@dataclass(frozen=True, slots=True)
class ListQuery:
    filter: Filter | None = None   # what the user asked for
    scope: Filter | None = None    # what their identity permits
    ...

    @property
    def effective_filter(self) -> Filter | None:
        return and_(self.filter, self.scope)
```

Keeping them apart is deliberate, and it is the single most load-bearing
decision in the codebase. Providers are required to read `effective_filter`,
never `filter`. A view that rebuilds the user's filters therefore *cannot*
drop the authorization restriction — the two live in different fields, and the
one that reaches the backend always includes both.

`Resource.build_query()` is the only sanctioned way to construct one, and it
fills in `scope` from the policy. Routes never build a `ListQuery` directly.

## Capabilities and the shim

Backends differ enormously. A Postgres table can filter, sort, paginate, count
and aggregate. A third-party JSON endpoint returns a list and nothing else.

Rather than write every view twice, each provider declares what it can do:

```python
class Capabilities:
    read, write, create, update, delete
    server_filter, server_sort, server_search, server_paginate
    total_count, aggregate
    write_mode: "sync" | "async"
    filter_ops: frozenset[Op]
```

`CapabilityShim` wraps any provider, emulates in Python whatever it declined,
and advertises the union. Views are written once, against a full contract.

Three rules make this safe:

**Pushdown is all-or-nothing per stage.** A backend that supports `eq` but not
`icontains` gets the *whole* filter emulated. Pushing half a filter tree and
evaluating the rest locally would need set-intersection semantics a row-based
pipeline cannot express.

**Pagination is only pushed down when filtering and sorting were too.**
Otherwise the backend slices rows 1–25 and the shim filters *those* — paging
over the wrong population. This produces plausible, wrong data rather than an
error, which is why it is checked explicitly.

**Emulated stages are stripped from the inner query.** Many HTTP clients ignore
parameters they do not understand; leaving `sort=` on a query we intend to sort
locally would let a provider that half-honoured it double-apply the ordering.

The shim's row budget (`max_rows`) is enforced by raising `TooManyRows`, never
by truncating. A short result set looks like a valid answer, which is worse than
an error.

`uv run crm capabilities` shows, per resource, which stages are native and
which are emulated. The same table is on the `/system` page.

## Providers compose

The three wrappers below all satisfy the same interface they wrap, which is why
a resource can gain any of them without a view noticing.

| Wrapper | What it changes |
| --- | --- |
| `CapabilityShim` | Emulates the query stages the backend cannot do. Applied to everything. |
| `AuditingProvider` | Records every write with before/after values. Sits inside the shim, so it sees real writes and not the reads the shim performs. |
| `CompositeProvider` | Reads from one backend, writes to another. |
| `ReadOnly` | Refuses writes. |
| `UnionProvider` | Merges several backends into one row set — see [Several databases](multiple-databases.md). Each source is shimmed on its own first, so the union pushes filters down rather than fetching everything and filtering itself. |

Where the tables behind those backends live is not configured twice.
`app.core.placement` reads it back out of the resource declarations — a
resource naming `db.archive#archived_deals` is what says that table is in the
archive — and `crm migrate` and `crm seed` act on one database at a time from
that. Tables nothing claims belong to the default connection, so a
single-database deployment declares no placement at all.

## Writes have three outcomes, not two

```python
class WriteStatus(StrEnum):
    OK = "ok"           # stored, confirmed
    PENDING = "pending" # accepted, not confirmed
    ERROR = "error"     # rejected
```

`PENDING` is what allows a message queue and a SQL table to share one form
pipeline. Publishing to a broker means the broker took the message — not that
the change was applied, and not that it ever will be. Reporting that as success
would tell the user something the next page load contradicts.

The UI renders a queued badge and a correlation reference instead.

## Fields produce canonical Python values

`Field.to_storage()` returns a real `date`, not an ISO string. Serialising in
the field would bake in one backend's wire format: a typed SQL column wants the
object, a JSON API wants the string, a message body wants the string.

Adapting is each provider's job — `SQLProvider._adapt()` coerces to the column
type, `RestProvider._jsonable()` serialises for the body. This split is what
lets one field declaration serve every backend.

## Request handling

One router serves every resource. A handler answers three audiences from one
body, decided from request headers:

| Request | Response |
| --- | --- |
| ordinary browser GET | full page |
| `HX-Request: true` | HTML fragment |
| `Accept: application/json` | JSON |

`View` (in `app/web/deps.py`) carries the request, identity, registry,
templates and response helpers, so handlers take one dependency rather than six.

### What the dependency does before the handler runs

`build_view` is the one thing every route resolves, which makes it the only
place a check cannot be forgotten. Three happen there rather than in handlers:

| Check | Why not in the handler |
| --- | --- |
| CSRF validation | a new route added without the call is not a test failure, it is a hole nobody notices — and one had been added that way |
| forced password change | not every route calls `require_login`; a list view checks `require_can` instead, and a gate some routes skip is not a gate |
| loading row-level grants | policy methods are synchronous, so their data must be fetched before any of them run |

The same reasoning applies at the middleware layer, which wraps error responses
too: a request id (`X-Request-ID`, tying a request to its audit rows and log
lines), a query count and timing, and compression. A request over the
configured thresholds logs one line naming the statement that repeated most —
which is how an N+1 announces itself. See [`scaling.md`](scaling.md).

### Saying something to the user

A message often accompanies a navigation: "Saved", "That link has expired", a
one-time password. HTMX can carry it back in a header and show it without
navigating; a plain form post cannot, because it answers with a redirect and
the browser discards the header with the rest of that response.

So `View.toast()` picks the channel: a header for HTMX, and otherwise a short-
lived signed cookie that the next page renders and clears. Getting this wrong
is not cosmetic — it makes a button that worked look like a button that did
nothing.

## Template resolution

Most-specific-first, cached unless template reload is on:

```
resources/<resource>/<view>.html   →   views/<view>.html
resources/<resource>/fields/display/<field>.html
  → fields/display/<widget>.html
  → fields/display/<type>.html
  → fields/display/text.html
```

Overriding one screen, or one column's rendering, never requires copying the
machinery behind it. This is what makes the project a starter rather than a
framework to fight.

## Authorization

Two separate questions, deliberately answered by different mechanisms:

| Question | Mechanism | Failure |
| --- | --- | --- |
| May they do this? | `Policy.allows(op, identity, record)` | 403 |
| Which rows exist for them? | `Policy.scope(identity)` → a `Filter` | invisible |
| May they see this field? | `Field.read_roles` | omitted |

The second is the important one. Because it is a filter folded into every
query, it holds on paths that never call `list()` — detail pages, edit forms,
inline cell saves, custom actions, CSV export, and the aggregates behind charts
and the dashboard. None of those routes contain authorization code.

A record outside the caller's scope reads as **404, not 403**: confirming that
a key exists is itself a disclosure.

## Three ways to do something later

They are not alternatives; each is right for a different promise.

| | What it is | Survives a restart? | Use it for |
| --- | --- | --- | --- |
| `asyncio` task | a coroutine on this worker's event loop | no | work nobody is waiting on and nobody was promised — a delivery, a cache warm |
| **job queue** | a row in `jobs`, drained by `crm worker` | yes | work that was *accepted*: an export somebody asked for, an outbound message |
| `crm notify-due` | a sweep run by cron | yes | work that is due at a time rather than caused by an event |

The queue is the middle one, and its design is in
[`scaling.md`](scaling.md#background-work-and-queues). Two things about it are
architectural rather than operational.

It is **a resource like any other** — `provider="db.main#jobs"` — so it is
visible as a screen, it works over any provider that supports a conditional
write, and its tests run against the in-memory provider rather than needing a
database. Nothing about it is specific to PostgreSQL.

And it is **at-least-once**, where the reminder sweep is at-most-once. That is
not an inconsistency: a reminder is an email, where a rare duplicate is worse
than a rare miss, and a job is code you wrote, which can be made idempotent.
Each picks the bargain that suits what it carries.

## Modules

**Almost nothing ships.** The default application is accounts, roles,
permissions, the audit trail and notifications — the parts every internal tool
needs and nobody wants to write twice. There are no business entities, because
yours are not ours.

| Module | Loads | Contains |
| --- | --- | --- |
| `core_identity` | always | users, API tokens |
| `core_access` | always | roles, permissions, audit log, notifications |
| `demo_crm` | opt-in | companies, contacts — the worked example |
| `demo_sales` | opt-in | deals, activities; board, calendar, charts, actions |
| `demo_remote` | opt-in | a REST-backed resource and a queued write |

`CRM_MODULES` names the *optional* modules to switch on. It is additive: the
two `core_` modules load whatever it says, so no configuration can produce an
application nobody is able to administer.

A module is a package with a `MANIFEST` and a `register(registry)` function,
discovered by directory scan and by entry point. It may also carry two files
the loader looks for by name:

| File | Purpose |
| --- | --- |
| `schema.py` | tables, declared against `app.schema.metadata` — so `crm migrate` and `crm seed` see them |
| `seed.py` | a `seed(ctx)` coroutine filling them with sample data |

Both are imported **only for modules that are enabled**, and that ordering is
load-bearing. Every discovered package is imported to read its manifest,
including ones that will not be enabled; if an optional module declared its
tables in `__init__.py`, merely existing on disk would put them on the shared
metadata and into the next generated migration. Keeping them in a submodule is
what makes a module a real unit of deployment rather than a naming convention.

Modules load in dependency order, so a module may extend resources and views
that an earlier one declared:

```python
def register(registry):
    registry.add_resource(deals)
    # add to a resource this module did not declare
    registry.resource("companies").view("list").add_columns("industry", after="name")
```

View specs are mutated in place rather than copied — an extension must affect
the instance the registry already holds, or the change would apply to a copy
nobody renders.
