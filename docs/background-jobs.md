# Background jobs

There are three ways to do something later, and they are not alternatives.
Each keeps a different promise.

| | What it is | Survives a restart? | Use it for |
| --- | --- | --- | --- |
| an `asyncio` task | a coroutine on this worker's event loop | no | work nobody is waiting on and nobody was promised |
| **the job queue** | a row in `jobs`, drained by `crm worker` | yes | work that was *accepted*: an export somebody asked for, an outbound message |
| `crm notify-due` | a sweep run by cron | yes | work due at a time rather than caused by an event |

This page is about the middle one.

## The shape of it

```python
from app.jobs import Cancelled, Retry, queue, register

@register("report.export")
async def export(job):
    deal_id = job.payload["deal_id"]
    ...
```

```python
await queue.enqueue("report.export", {"deal_id": 42}, key="export:42")
```

```bash
uv run crm worker              # drain the queue until stopped
uv run crm jobs                # what is queued, running, done, failed
uv run crm jobs --failed       # and what broke
```

The row is written **before** the caller is told the work was accepted. That
is the entire reason the table exists: an `asyncio` task is faster and is lost
when the worker stops, which is fine for a delivery nobody is waiting on and
not fine for one somebody was told had been accepted.

## What a handler may raise

| Raise | Meaning | What happens |
| --- | --- | --- |
| nothing | it worked | `done` |
| `Retry("reason")` | another attempt might succeed | back to `queued`, with backoff |
| `Retry(..., delay=30)` | and here is how long to wait | back to `queued`, at that delay |
| `Cancelled("reason")` | this should not run after all | `done`, with the reason recorded |
| anything else | a bug | `failed`, immediately |

That last row is the one to understand. A `KeyError` in a handler will produce
the same `KeyError` five times and delay the report of a real bug, so only
failures a handler explicitly asks to retry are retried. A handler wrapping
something flaky is responsible for translating: catch the timeout, raise
`Retry`.

`Cancelled` is not a failure. A job for a record somebody deleted in the
meantime has nothing to do, and reporting that as broken trains people to
ignore the failed list.

## Four properties, and the failure each prevents

**Claimed, not locked.** A worker takes a job with a conditional write and
stamps its name on it. Two workers reading the same row at the same moment
means one of them wins the write and the other moves on. No advisory locks and
no `SELECT ... FOR UPDATE`, so any provider that supports `update_if` can hold
the queue — which is also why the tests run in memory rather than needing a
database.

**Attempts are counted at the claim, not at the failure.** Counting at the
failure feels more accurate: you only used an attempt if something went wrong.
But a job that segfaults its worker never reaches the failure path, so it would
retry for ever and take the queue with it. "A worker took this and did not come
back" has to cost an attempt.

**A lease, not a prayer.** A worker killed between claiming and finishing
leaves a row marked `running` that nothing will ever finish. After
`--lease` seconds (900 by default) it is assumed dead: requeued if it has
attempts left, parked as `failed` if it does not.

**At-least-once, not exactly-once.** A job that succeeds and dies before
recording it will run again. **Handlers must be idempotent** — the queue cannot
make them so.

That last one is the opposite bargain from `crm notify-due`, which claims each
reminder *before* sending so a reminder is never duplicated. The difference is
deliberate: a reminder is an email, where a rare duplicate is worse than a rare
miss, whereas a job is code you wrote and can make safe.

## Retries and backoff

Doubling from 30 seconds, capped at an hour: 30s, 1m, 2m, 4m, 8m. Slower than a
notification's in-request retry because nobody is waiting — the cost of
patience here is a delay, not a user watching a spinner. Five attempts by
default; pass `max_attempts` to change it.

There is no `retrying` state, deliberately. A job waiting to be retried is
`queued` with a later `run_at`, so one indexed query finds everything eligible
and a retry needs no timer anywhere.

## Idempotency keys

```python
await queue.enqueue("report.export", {...}, key="export:42")
```

A **queued or running** job with the same key is returned instead of a second
one being created. Only against unfinished work, deliberately: a job that has
already run and is enqueued again with the same key is a new request to do the
thing again, not a duplicate of the finished one.

## Jobs with no handler are parked, not lost

A kind nothing handles is left `queued` and logged once, rather than claimed
and failed. A deployment may enqueue work for a handler that ships in the next
release, and burning its attempts in the meantime would park work that was
going to be fine.

## Running it

```bash
uv run crm worker --concurrency 4 --poll 1.0
docker compose --profile worker up
docker compose --profile worker up --scale worker=3
```

The Docker image runs it: there is no `ENTRYPOINT`, so `command: ["crm",
"worker"]` replaces the web server and the same image serves both roles. One
thing to carry over if you write your own service — the image's `HEALTHCHECK`
curls `/healthz`, which a worker does not serve, so disable it or the container
reports itself unhealthy for ever:

```yaml
worker:
  build: .
  command: ["crm", "worker"]
  healthcheck:
    disable: true
```

Its own process, on purpose. Work that must survive a deploy should not live
inside the thing being deployed, and a process that is not serving requests can
be sized and restarted on its own. Several workers are safe without
coordinating — `docker compose up --scale worker=3` needs no configuration,
because claiming is what keeps them apart.

Stop it with Ctrl-C or `SIGTERM`: it finishes the jobs in hand and returns.
Anything killed outright is picked up again once its lease expires.

The worker **polls** — one indexed query per idle second per worker. Cheap, and
not instant: a job enqueued now starts within the poll interval.

## Where the queue lives

`jobs` is a resource, so it names a connection like any other. By default that
is the application's own database:

```bash
CRM_JOBS_CONNECTION=db.main       # the default
CRM_JOBS_CONNECTION=db.queue      # its own database
```

Pointing it elsewhere is worth doing once a queue is busy. A worker polls once
a second per process; that is a steady write load with no reason to share a
connection pool with the requests people are waiting on. The connection is
declared in `connections.yaml` like any other, and because the placement map is
derived from the resource declarations, `crm migrate --all` then covers the
queue's database too without being told about it separately.

### On Redis instead

The queue needs exactly one thing from its backing store: a **conditional
write**. `update_if` is what makes claiming safe, and it is the only reason two
workers cannot run the same job. Any provider offering it can hold the queue —
which is why the tests in `tests/test_jobs.py` run against `MemoryProvider` in
milliseconds rather than needing a database.

Redis has a real one, in `WATCH`/`MULTI`, and a Redis provider ships in the
companion [`crm_starter_modules`][modules] repository:

```bash
uv pip install 'crm-starter-modules[redis]'
CRM_MODULES=db_redis CRM_JOBS_CONNECTION=redis.jobs uv run crm worker
```

Nothing in `app/jobs/` changes. The claim query becomes one set intersection and
one sorted-set range instead of a table scan, which is what makes it worth
doing on a busy queue.

**It is not the durable choice.** Redis persistence is `RDB` snapshots or `AOF`
with an fsync policy, and the default policy loses about a second of writes on
a hard stop. For a cache that is the correct trade-off — which is exactly what
Redis is already used for here. For a queue whose whole purpose is that an
accepted job is not lost, "usually durable" is the property being paid to
avoid. Run it with `appendonly yes` and `appendfsync always` if the jobs
matter, and know that it is still weaker than the database you already run.

So: Redis is the answer to **throughput**, not to durability, and
`CRM_JOBS_CONNECTION=db.main` remains the right default. Past the point where
even that is not enough, a broker beats a Redis list, and `AMQPWriteProvider`
is already the shape of it.

[modules]: https://github.com/Sunyata-OU/crm_starter_modules

## Queued notification delivery

The first thing to use the queue, and the seam the design always named.
`CRM_NOTIFY_DELIVERY` chooses how a notification reaches its channels:

| Value | Delivery | Survives a restart? |
| --- | --- | --- |
| `background` (default) | a task on this worker's event loop | no — in-flight ones are lost |
| `queue` | a row, drained by `crm worker` | yes |
| `inline` | before the request returns | n/a — what tests and CLI commands want |

Nothing upstream changes. The notification is still stored first, the channels
are still the channels; only where the delivery runs is different.

Two details worth knowing:

* **The job carries the notification's id, not its contents.** A job is a
  pointer to work, not a copy of it, so a notification amended or dismissed
  between enqueueing and delivery is handled as it now is — and the queue does
  not become a second place the same data lives.
* **A queue that cannot accept the job falls back to delivering normally.**
  Losing durability must not mean losing the notification.

!!! warning "A queue with nothing draining it delivers nothing"
    `CRM_NOTIFY_DELIVERY=queue` without a worker running stores every
    notification and sends none of them — silently, since neither the
    application nor the channels error. `crm serve` warns about the combination
    in production. The in-app bell still works throughout, because it reads the
    stored row.

## Looking at the queue

`jobs` is a resource like any other, so it has a screen: **Administration →
Background jobs**, admin-only, read-only apart from a *Queue again* action.

That action resets the attempt count, which is the point — a job that failed
five times becomes unretryable exactly at the moment somebody has fixed the
reason it failed. It is offered only on finished jobs: requeueing one a worker
currently holds would put two workers on it, which is the one thing claiming
exists to prevent.

## Adding your own

A module registers handlers the same way it registers anything else — on
import, from its `__init__.py`:

```python
from app.jobs import Retry, register

@register("invoices.send")
async def send_invoice(job):
    try:
        await billing.send(job.payload["invoice_id"])
    except TimeoutError as exc:
        raise Retry(str(exc)) from exc
```

Two rules. Make it **idempotent**, because it may run twice. And keep the
payload a **reference**, not a copy — an id the handler reads the current state
from, rather than a snapshot that may be stale by the time it runs.
