# Scaling and performance

Every `CRM_` variable named here is listed with its default in
[`configuration.md`](configuration.md).

Two different questions, answered in order: how to know what a page costs, how
to make one host handle more, and how to run more than one host.

## Measuring first

Performance work without a number is guesswork, and for an application shaped
like this one the number is almost always **queries per request**. A page that
issues one query per row looks fine on a demo and collapses on real data; a
page that issues a fixed number stays flat forever.

Every engine the application opens is instrumented, and the count is kept per
request. In `debug` mode it comes back on the response:

```bash
curl -sI localhost:8000/r/contacts | grep -i 'query-count\|server-timing'
X-Query-Count: 3
Server-Timing: db;dur=1.2, total;dur=6.6
```

In production the headers are off and the **slow-request log** stays on. A
request over `CRM_WARN_QUERIES` (default 25) or `CRM_WARN_REQUEST_MS` (default
1000) logs one line naming the slowest statement and the most-repeated one:

```
slow: GET /r/deals in 210ms (63 queries, 190ms in the database);
  slowest 40ms: SELECT deals... ; repeated 50x: SELECT companies.name FROM companies WHERE id = ?
```

`repeated 50x` on a page showing fifty rows is an N+1 and needs no further
diagnosis.

The same mechanism is available to tests, which is how `tests/test_performance.py`
pins the cost of each view and fails when it changes:

```python
with measure() as stats:
    ...
assert stats.queries == expected
```

### What the shipped views cost

Measured against SQLite with the demo modules enabled, caches warm:

| Page | Queries | Shape |
| --- | --- | --- |
| contacts, list | 3 | rows, count, one batch per relation field |
| company, detail | 8 | record, timeline, backref lists, relation labels |
| deals, board | 8 | one grouped aggregate + one per column (5) + one batch per relation field (2) |
| activities, calendar | 2 | rows, one relation batch |
| dashboard | 10 | one count per visible resource, issued concurrently — 24ms of database time in 8ms of wall clock |
| all deals (union of two databases), list | 4 | rows + count, once per database, issued concurrently |
| the same, filtered to one store | 2 | a filter on the source column picks the databases; the other is never opened |
| the same, chart or pivot | 2 | each database groups its own rows; only the groups are merged |

None of these grow with the number of rows on the page. That property is what
the performance tests exist to protect.

## One host

**Use PostgreSQL.** SQLite has a single writer; two concurrent writes serialise
and a third waits. It is an excellent development and small-deployment
database and a poor choice for anything with concurrent editors.

**Size the pool to the work, not to a round number.** In `connections.yaml`:

```yaml
pool_size: ${CRM_DB_POOL_SIZE:-10}
max_overflow: ${CRM_DB_MAX_OVERFLOW:-20}
pool_recycle: ${CRM_DB_POOL_RECYCLE:-1800}
```

`pool_size` should be at least the number of requests one worker handles
concurrently. Across every worker and every host, `pool_size + max_overflow`
must stay under the database's own connection limit — exceeding it turns a
traffic spike into refused connections. `pool_recycle` must sit below any idle
timeout in the database or a proxy in front of it, or a pooled connection is
eventually handed out already dead.

**Run several workers.** The application is async, but a single worker is a
single Python process and will saturate one core. Almost all of a request is
rendering, not waiting — the database is a millisecond or two of a twenty
millisecond request — so throughput is bounded by CPU, and the fix is
processes:

```bash
uv run uvicorn app.main:get_app --factory --workers 4
```

Measured on one host, demo data, SQLite, 30 concurrent clients:

| Page | 1 worker | 4 workers |
| --- | --- | --- |
| contacts list | 180 req/s | 460 req/s |
| deals board | 108 req/s | 290 req/s |

Close to linear, and it is worth knowing why it is not exactly linear: each
worker warms its own permission cache and its own reflected table metadata, and
SQLite serialises writers. Four workers is a reasonable starting point on a
four-core host. More workers than cores buys nothing and multiplies the
connection pool.

**Indexes.** The declared schema indexes what the shipped views filter and sort
on. A resource of your own needs the same attention: any field with
`in_filter=True`, `sortable=True`, or a role's row scope pointing at it will be
in a `WHERE` clause on every request.

**Turn template reloading off.** `CRM_TEMPLATE_RELOAD=false` in production;
`crm serve` refuses to start without it.

**Static files.** Served with a `Cache-Control` lifetime of
`CRM_STATIC_MAX_AGE` (default one hour, deliberately short because the files
have stable names). Behind a CDN, add a content hash to the filenames and raise
it to a year.

**Compression.** Responses over `CRM_GZIP_MIN_SIZE` bytes are gzipped at
`CRM_GZIP_LEVEL`, which defaults to 6 rather than zlib's 9. On a 45KB page
that is 4.2KB instead of 4.1KB for a quarter of the CPU — compression was
otherwise one of the more expensive things a request did.

## Caching

Off by default. Turn it on in `connections.yaml`:

```yaml
cache.main:
  type: cache
  backend: memory     # or: redis
  url: ${CRM_REDIS_URL}
```

### What is cached

| What | Key | Lifetime | Why it is safe |
| --- | --- | --- | --- |
| permission grants | one key for the table | TTL, dropped on any edit | a grant edit goes through this application, so there is an exact invalidation point |
| relation labels | resource + primary key | one request | a label is only shown after the policy has already allowed the target resource |
| reflected table metadata | connection + table | process lifetime | the schema changes at deploy time, not at request time |
| compiled templates | Jinja's own | process lifetime | source files do not change under a running server |

### What is deliberately not cached

**Records.** It is the obvious thing to add and the wrong thing to add. Two
reasons, and the second is the serious one.

Invalidation: this application is not the only writer. Another service, a
migration, someone at a psql prompt — a cache keyed on a query has no way to
learn that any of them happened, so the stale window is unbounded rather than
merely long.

Scoping: what a row *is* depends on who is asking. Row-level scope means two
people issuing the same query legitimately get different results, and per-field
permissions mean the same row renders differently for each of them. A cache
keyed on the query is then a cache keyed on the wrong thing, and the failure
mode is not a stale number on a screen — it is one person seeing another's
records. A correct key would have to include the full identity and its grants,
at which point the hit rate is close to zero and the mechanism has paid for
nothing.

If a particular query is genuinely expensive and genuinely shared, cache it
where the answer is known: in a provider, or in a materialised view in the
database, both of which can be invalidated by whatever writes to them.

### The rule every backend follows

**A cache outage makes the application slow, never broken.** Every read and
write fails soft, and `get_or_set` falls through to computing the value. That
is asserted in `tests/test_cache.py` against a backend whose every operation
raises, because a cache that is load-bearing has stopped being a cache.

### Why the shared one is worth configuring

A memory cache is per worker: four workers hold four copies and can disagree
for up to the TTL. For a relation label that is harmless. For a permission
grant it means a revoked permission still works on three hosts.

With a shared cache the grant table lives under one key. An edit deletes that
key rather than one worker's memory, so the next request on any host reloads —
and the in-process copy drops to a two-second life, just enough to keep the
common case off the network.

## Background work and queues

There are two separate things here, and only one of them is a queue.

**Writes through a queue** are already a provider: `AmqpWriteProvider` turns a
create or an update into a message and returns `PENDING` with a correlation id,
which the form pipeline renders as a queued badge. That is how a resource whose
writes are asynchronous shares its screens with one whose writes are not.

**Background jobs** are a table and a worker. `queue.enqueue("kind", {...})`
writes a row before the caller is told the work was accepted; `crm worker`
claims and runs it. The row is the point: an `asyncio` task is faster and is
lost when the worker stops, which is fine for a delivery nobody is waiting on
and not fine for one somebody was told had been accepted.

```python
from app.jobs import Retry, register, queue

@register("report.export")
async def export(job):
    ...                      # raise Retry(...) to ask for another attempt

await queue.enqueue("report.export", {"id": 42}, key="export:42")
```

```bash
uv run crm worker              # drain the queue until stopped
uv run crm jobs --failed       # what is queued, running, done, failed
```

Four properties, each the answer to a way this normally goes wrong:

| | |
| --- | --- |
| **Claimed, not locked** | A conditional write, so several workers never run the same job — and no advisory locks, so any provider that supports `update_if` can hold the queue. |
| **At-least-once** | A retried job may run twice, so **handlers must be idempotent**. The opposite bargain from `deliver_due`, deliberately: a reminder is an email, where a duplicate is worse than a miss, but a job is code you wrote and can make safe. |
| **Attempts counted at the claim** | A worker killed mid-job has still used one. Counting at the failure instead is how a job that reliably kills its worker runs for ever. |
| **A lease, not a prayer** | A job whose worker vanished is requeued once its claim goes stale (15 minutes by default), or parked as failed if it is out of attempts. |

Only `Retry` is retried. Any other exception fails the job at once, because
repeating a `KeyError` five times produces the same `KeyError` and delays the
report of a real bug.

**Notification delivery** is the first thing to use it. `CRM_NOTIFY_DELIVERY`
chooses:

| Value | Delivery | Survives a restart? |
| --- | --- | --- |
| `background` (default) | a task on this worker's event loop | no — in-flight ones are lost |
| `queue` | a row, drained by `crm worker` | yes |
| `inline` | before the request returns | n/a — what tests and CLI commands want |

`background` awaits in-flight deliveries at shutdown, bounded by
`CRM_SHUTDOWN_TIMEOUT`; a worker killed outright still loses them, though the
notification row survives so nothing is lost from the in-app bell. `queue`
survives all of that **but needs `crm worker` running somewhere** — with
nothing draining it, notifications are stored and never delivered, which is why
`crm serve` warns about the combination in production.

Delivery is retried on transient failures only: a timeout, a dropped
connection, an SMTP 4xx or an HTTP 5xx. A rejected address or a 404 webhook is
reported at once rather than attempted three times.

Scheduled work is still a sweep, not a timer: `crm notify-due`, run by cron. It
claims each row with a conditional write before sending, so running it from
several places is safe.

Run the worker as its own process. Work that must survive a deploy should not
live inside the thing being deployed, and a process that is not serving
requests can be sized and restarted on its own.

## Several hosts

The application holds no per-user state in memory, so it scales horizontally
without sticky sessions — sessions are signed cookies, read by any host. Four
things need attention before you add the second host.

**File storage must be shared.** The default local-disk store is per-host: an
upload handled by host A is a 404 on host B. Switch to S3-compatible storage,
which is three lines in `connections.yaml`:

```yaml
files:
  type: files
  backend: s3
  bucket: ${CRM_S3_BUCKET}
```

**Run the reminder sweep from one scheduler**, or accept that it is safe not
to. `crm notify-due` claims each row with a conditional write before sending,
so two sweepers cannot both deliver the same reminder — but only over a backend
that supports conditional writes. If yours does not, the sweep says so in the
log, once, and you should run exactly one.

**Permission changes propagate within `CRM_PERMISSION_CACHE_TTL`** (default 30
seconds). Every request consults the grant table, so it is cached per worker;
the worker that handles an edit invalidates its own cache immediately, the rest
notice within the TTL. Shorten it if that window matters, or accept it: it is
the one place this design trades immediacy for not requiring Redis.

**Background delivery is best-effort — unless you queue it.** A notification
delivered in the background is a task in the worker's event loop; a worker that
stops takes any in-flight delivery with it. The notification row itself is
already stored — written before delivery is attempted, deliberately — so
nothing is lost from the in-app bell, only the outbound copy. Set
`CRM_NOTIFY_DELIVERY=queue` and run `crm worker` to make the outbound copy
durable too.

**Run at least one worker**, and more than one if the work justifies it. Jobs
are claimed with a conditional write, so workers do not need to coordinate, and
a job whose worker dies is picked up by another once its lease expires.

## Known limits

Stated rather than hidden, because a starter should be honest about where it
stops:

- **A queued job runs at least once, not exactly once.** A handler that
  succeeds and dies before recording it will run again. Handlers must be
  idempotent; the queue cannot make them so.
- **The worker polls.** One indexed query per idle second per worker — cheap,
  and not instant. A job enqueued now starts within the poll interval.
- **Sessions cannot be revoked before they expire.** They are signed cookies
  with no server-side record, so "sign this user out everywhere" is not
  possible without adding one. `CRM_SESSION_MAX_AGE` bounds the exposure.
- **The audit log has no retention policy.** It grows without limit. Add a
  scheduled delete, or partition it, before it becomes the largest table.
- **The capability shim has a ceiling.** A backend that cannot filter has its
  rows filtered in Python, bounded by `CRM_MAX_LOCAL_ROWS` (default 5000).
  Beyond that it raises rather than silently truncating. That is a correctness
  guarantee, not a performance one: if you are hitting it, push the work into
  the backend instead.
- **A board costs one query per column.** Bounded by the number of choices in
  the grouping field, not by the number of rows, but a status field with thirty
  values makes an expensive board.
- **A union reads a full page from every source.** Serving page N of merged,
  sorted results needs `offset + page_size` rows from each backend, so paging
  deep into a union is the one thing that costs more than a single-source view.
  Bounded by `max_rows` and refused beyond it. See
  [`multiple-databases.md`](multiple-databases.md).
- **A union cannot average across its sources**, and cannot be written to. Both
  are refusals with a message rather than approximations.
