# CRM Starter

A scaffold for building internal CRM-style applications with **FastAPI, Jinja
and HTMX**.

You declare a resource and its fields. You get list, form, detail, board,
calendar, chart and pivot views, filters, search, sorting, pagination,
inline-editable cells, CSV export and a JSON API — without writing a route, a
query or a template.

The part that makes it more than a code generator: **every backing store is a
provider**. A resource can be a Postgres table, a read-only REST endpoint, a
RabbitMQ queue, or a combination — and the screens are identical either way.

```python
Resource(
    "contacts",
    provider="db.main#contacts",
    fields=[
        TextField("name", required=True, searchable=True, inline_editable=True),
        EmailField("email", required=True, searchable=True),
        RelationField("company_id", resource="companies", display="name"),
        StatusField("status", choices=[("lead", "Lead"), ("active", "Active", "green")]),
    ],
)
```

That is a complete, working screen.

## What ships, and what does not

Almost nothing ships. The default application is accounts, roles, permissions,
the audit trail and notifications — the parts every internal tool needs and
nobody wants to write again. There are no business entities, because yours are
not ours.

```bash
uv sync
uv run crm seed          # accounts, roles and grants. Nothing else.
uv run crm dev           # http://127.0.0.1:8000
```

The CRM in the name is a **demo**, in two optional modules you turn on to look
at and delete when you have seen enough:

```bash
CRM_MODULES=demo_crm,demo_sales uv run crm seed
CRM_MODULES=demo_crm,demo_sales uv run crm dev
```

| Module | Ships enabled? | Contains |
| --- | --- | --- |
| `core_identity` | always | users, API tokens |
| `core_access` | always | roles, permissions, audit log, notifications |
| `demo_crm` | opt-in | companies, contacts |
| `demo_sales` | opt-in | deals, activities — board, calendar, charts, custom actions |
| `demo_remote` | opt-in | a REST-backed resource and a queued write |
| `demo_archive` | opt-in | one resource served from two databases at once |

`CRM_MODULES` is additive: it names the optional modules to switch on. The two
`core_` modules load regardless, so no setting can produce an application
nobody is able to administer.

Sign in with `admin@example.com` / `demo-password`. Two other accounts show
row-level scoping in action once the demo is on:

| Account | Sees |
| --- | --- |
| `admin@example.com` | everything, plus the System page |
| `manager@example.com` | every deal |
| `kim@example.com` | only the deals they own |

## What you get for a declaration

| | |
| --- | --- |
| **Views** | list, form, detail, **board/kanban**, calendar, chart, pivot |
| **Query** | filters in the URL, free-text search, multi-key sort, pagination |
| **Editing** | full forms, modal forms, double-click inline cells, bulk actions |
| **Fields** | 27 types, each with coercion, validation, display and input rendering |
| **Access** | database-driven RBAC, per-field visibility, row-level scoping applied to *every* read |
| **History** | every write recorded with before/after, per-record timeline, full audit log |
| **Auth** | password, OIDC/SSO, gateway headers, API tokens — chained |
| **Files** | uploads to local disk or S3-compatible storage, permission-checked downloads |
| **Notifications** | in-app bell, email, webhooks, and scheduled reminders |
| **Background jobs** | a durable queue with claims, retries, backoff and leases — `crm worker` |
| **Passwords** | change, admin reset, emailed single-use reset links, lockout — *only where the auth provider owns the password* |
| **Caching** | optional, shared or in-process, off by default |
| **Several databases** | a resource per database, or **one resource merged from several**, each migrated and seeded on its own |
| **Mobile** | one stylesheet: lists become cards, the sidebar becomes a drawer, matrices scroll |
| **Also** | CSV export, JSON API, typeahead relations, quick filters, modal forms, migrations, health checks, per-request query counts |

## The idea in one paragraph

Three registries stack on each other. **Connections** are named handles to
external systems, opened lazily. **Providers** turn a connection into something
that answers the query language, and each declares what it can do server-side.
**Resources** name a provider and list their fields; the views are derived from
those. Between providers and resources sits the *capability shim*: whatever a
backend cannot do — filter, sort, count, aggregate — it emulates in Python, so
every view is written once against a full contract and works over any backend.
Because providers compose, a resource may name several at once: `Union` merges
two databases into one set of screens, and nothing above it knows.

## Commands

```bash
uv run crm dev                 # development server
uv run crm serve               # production server (refuses a bad config)
uv run crm seed                # create and populate the demo database
uv run crm seed -c db.archive  # ... a second database, its tables only
uv run crm resources -v        # what is registered, and every field
uv run crm capabilities        # what each backend does natively vs. emulated
uv run crm check-connections   # open every connection and report health
uv run crm routes              # every URL served
uv run crm token ci-runner     # mint an API token
uv run crm new-resource orders # print a declaration to start from
uv run crm migrate             # bring the database up to date
uv run crm migrate --all       # ... every database, if there is more than one
uv run crm make-migration "…"  # generate one from the model
uv run crm passwd you@example  # set an account's password from the shell
uv run crm worker              # run queued background jobs until stopped
uv run crm jobs --failed       # what is in the queue, and what broke
uv run crm notify-due          # deliver reminders that have come due
uv run crm notify-test you@…   # check the notification channels
```

## Docker

```bash
docker compose up                      # app + PostgreSQL on :8000
docker compose run --rm seed           # load the sample data
docker compose --profile storage up    # add MinIO, to try the S3 backend
docker compose --profile mail up       # add Mailpit, to try email notifications
docker compose --profile worker up     # add a job worker to drain the queue
```

The image is multi-stage, runs as a non-root user, and carries a healthcheck
against `/healthz`. Uploads live on a named volume so they outlive the
container.

One image, three roles. There is no `ENTRYPOINT`, so the command replaces the
web server: `crm seed`, `crm worker`, `crm notify-due`. A container that is not
serving HTTP should disable the inherited healthcheck, which curls `/healthz`
and would otherwise report a healthy worker as unhealthy for ever — the
`worker` and `notifier` services show it.

## Adding a resource

1. Create `modules/my_module/__init__.py` with a `MANIFEST` and a
   `register(registry)` function — `uv run crm new-resource orders` prints one.
2. Declare a `Resource` and add it to the registry.
3. Restart. It appears in the navigation with a full set of screens.

A module package may also carry two files the loader looks for by name:

| File | Purpose |
| --- | --- |
| `schema.py` | tables, declared against `app.schema.metadata` — picked up by `crm migrate` and `crm seed` |
| `seed.py` | a `seed(ctx)` coroutine filling them with sample data |

Only enabled modules have these imported, which is why an optional module's
tables stay out of a migration generated without it.

Modules load in dependency order and may extend one another, so a module can
add a column or a field to a resource another module declared without editing
its source. `modules/demo_crm` is the worked example; `modules/demo_sales`
shows one module extending another's screens.

## Customising a screen

Templates resolve most-specific-first, so overriding one screen never means
copying the machinery behind it:

```
app/templates/resources/contacts/list.html   ← yours, if present
app/templates/views/list.html                ← the generic one
```

The same applies per field, per resource:

```
app/templates/resources/deals/fields/display/amount.html
app/templates/fields/display/currency.html
```

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — how the layers fit together
- [`docs/configuration.md`](docs/configuration.md) — every setting, and which file it belongs in
- [`docs/providers.md`](docs/providers.md) — writing a provider for your own backend
- [`docs/multiple-databases.md`](docs/multiple-databases.md) — resources across several databases, and one resource merged from several
- [`docs/background-jobs.md`](docs/background-jobs.md) — the durable queue, the worker, and what a handler may raise
- [`docs/resources.md`](docs/resources.md) — the declaration reference
- [`docs/auth.md`](docs/auth.md) — the four auth providers, RBAC and the audit trail
- [`docs/files-and-notifications.md`](docs/files-and-notifications.md) — file storage backends and notification channels
- [`docs/scaling.md`](docs/scaling.md) — measuring cost, tuning one host, running several

## Testing

```bash
uv run pytest
```

The important suite is `tests/contract/`. Every provider — in-memory, SQL,
REST, queue-backed, and each of those with its capabilities deliberately
stripped — runs the *same* tests and must produce identical results. That suite
is the specification; if behaviour is not asserted there, the views cannot rely
on it.

```bash
uv run ruff check . && uv run mypy app
```

## License

[MIT](LICENSE). Use it, fork it, ship it.
