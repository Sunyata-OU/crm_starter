# Notes and tasks

Two things a record page needs once the application has a database of its own:
somewhere to say *why*, and somewhere to write down *who is going to do
something about it*.

Both address a record the way a notification does — by resource name and record
id, as strings — so both work against a record this application only reads. A
note can hang off a company owned by another service, in another database,
through a connection that would refuse the write.

## The activity panel

`DetailView(timeline=True)` — the default — puts a panel beside the record
holding one chronology from two sources:

* **changes**, read from the audit log, so the history is a by-product of the
  writes themselves and cannot drift from them, and
* **notes**, written from the panel itself.

A pinned note is lifted above the history: "read this before you do anything
with this record" is not something to find by scrolling.

The panel needs `db.main`. Where there is none, set `CRM_TIMELINE=false` and no
record page offers it, for a deployment with no database of this
application's own.

### Notes

Written by anyone who may **read** the record. Deliberately not `update`: a
note changes nothing about the record, and requiring update permission would
mean the read-only screens — most of them, in a back office over other
services' data — could never be discussed.

An author may remove their own note; an administrator may remove any. Nothing
edits one in place: a conversation somebody else can rewrite is not a record of
anything.

### Mentions, and who hears about a note

`@somebody@example.com` addresses a person directly; a bare `@handle` is
resolved against other people. A handle that matches nothing is dropped rather
than guessed at, because a notification sent to a misspelling is one nobody
receives and everybody assumes was received.

**Where a staff directory exists**, a mention -- address or handle alike --
resolves *only* against it: a note is an internal conversation, and
`@anyone@anywhere` used to be taken at face value, which meant a note could
notify an address that had never touched the back office. A module
declares one by setting `registry.staff_directory`:

```python
from app.core.registry import StaffDirectory

registry.staff_directory = StaffDirectory(
    "accounts",                    # the resource listing staff
    roles=("admin", "manager"),    # who may search it; empty = any signed-in caller
    email_field="email",           # these four are the defaults
    username_field="username",
    name_fields=("firstName", "lastName"),
    sort_field="username",         # a sort the backend can honour
)
```

Somebody already on the
record only counts as a mention target when they are themselves in the
directory; being first to comment does not let an outsider back in.

The directory is cached for five minutes (`notes.DIRECTORY_TTL`), since it is
consulted on every note write and every `@` keystroke in the autocomplete
below. A directory that cannot be read never fails the write: the note
saves, the mention resolves to nobody, and the failure is logged rather than
surfaced to whoever was writing the note.

**Where there is no directory** -- a plain install, or no module declaring one -- mentions keep their original, broader behaviour: an address is
taken as written, and a bare handle is resolved against the people already on
the record, then against the account table where the deployment has one.

**Autocomplete.** `GET /r/<resource>/<pk>/notes/mentions?q=` backs the `@` box
in the note form and returns directory entries matching `q`. It is gated
exactly like the directory itself -- read on the record, and the caller must
themselves hold one of the directory's `roles` -- so a caller who could not
open the roster's own screen cannot list staff through the mention box either.

Beyond the people named, a note reaches everyone who has **already written on
the same record**. That is the whole subscription model: no follower table, no
subscribe button, and nothing to keep in step — somebody who commented on a
record has shown they care what happens to it. Nobody is ever told about their
own note.

### Attachments

A note may carry up to `notes.MAX_FILES` files. They go to the default file
store — local disk in development, S3 in the cluster, one line of
configuration apart — under `notes/<resource>/<record>/`, and are served
through `/files`, which requires a signed-in caller. The store's name is
recorded beside each file so a link written today still resolves after the
default changes.

Files are stored **before** the note row is written, and deleted **after** it:
an orphaned object in a bucket is housekeeping, whereas a note pointing at
bytes that were never stored is a broken link somebody has to explain.

## Tasks

`tasks` is work a person owes. Not to be confused with the `jobs` table, which
is the machine's queue: a job is claimed, retried and abandoned on a timeout,
and none of those verbs mean anything for something a colleague promised to do.

Assignment is manual, always. Nothing picks an assignee, round-robins, or
claims a task on somebody's behalf — a queue that assigns itself is a queue
nobody feels responsible for. What is automatic is being told.

A record page offers "add a task about this record", which opens the task form
with `resource` and `record_id` already filled in.

### The sweep

`crm tasks-sweep` announces two things:

| It notices | By comparing | And says |
| --- | --- | --- |
| a hand-over | `assignee` against `notified_assignee` | "Assigned to you" |
| a due date | `due_at` against now, once `reminded_at` is unset | "Due soon" / "Overdue" |

A task that is due and belongs to nobody goes to `CRM_TASK_WATCHERS`. Where
that names nobody, the task simply waits — which is honest, where picking
somebody arbitrarily would not be.

Notifying from a sweep rather than from the write is the design decision worth
knowing about. A task can be assigned from the form, an inline edit, a bulk
action or a script, and a notification raised at each of those places is one
that will eventually be forgotten at one of them. The sweep reads two columns
and announces the difference, so whatever made the change — and whether or not
the process that made it survived — the person finds out, exactly once.

Run it beside `crm notify-due`, from cron, a scheduler, or the `notify` profile
in `compose.yaml`.

## Recording who created a record

```python
Resource("tasks", stamp={"created_by": "id", "created_by_name": "label"}, ...)
```

Applied in the provider, below the audit wrapper, so every path that creates a
record stamps it — the form, an action, the API, a script — and the audit entry
records what was actually written. `id` is the address notifications are
addressed to (email, falling back to the subject); the other sources are
`label`, `email` and `subject`. A value the caller supplied is never
overwritten: an import carrying its own "raised by" is stating a fact the
stamp cannot improve on.

## Work raised by other systems

Other systems can raise tasks when something needs a person, and close them
when that stops being true. They do it by `POST /events/tasks` with a bearer
token that has the `integration` role (issue one from Administration → API
tokens), ideally from a transactional outbox on their side, so a message
survives a restart and is retried. Nothing here polls.

```json
{"type": "raise", "key": "crm:contact:7:review",
 "title": "Contact needs review: Ada", "body": "…",
 "resource": "contacts", "record_id": "7", "priority": "normal"}
{"type": "resolve", "key": "crm:contact:7:review"}
```

Both are idempotent on `key` (`tasks.source_key`, unique among open tasks), so a
retry after a lost response does not raise the work twice. Raising returns 201,
a repeat 200 `duplicate`.

**Who is told.** A task nobody owns is announced once to everybody in the staff
directory (`Registry.staff_directory`, or `CRM_TASK_WATCHERS` when it names
somebody) and stays in their bell. The moment it has an assignee -- by the form,
the *Assign to me* action, an inline edit, anything that writes through the
provider -- the announcement is marked read for everybody and only the assignee
is told. Finishing or cancelling withdraws it for good. This is
`AnnouncingProvider`, wrapped on the tasks resource via `Resource.provider_wrap`;
the sweep has no part in it beyond due-date reminders.

Needs `crm migrate` (adds `tasks.source_key`).
