"""The platform schema: accounts, access control, audit, notes and tasks.

Only tables the framework itself needs live here. Business tables belong to the
module that declares them -- see ``modules/demo_crm/schema.py`` for the shape --
and attach themselves to this same :data:`metadata` when the module is imported,
so Alembic and ``create_all`` see whatever set of modules is enabled.

Declared in SQLAlchemy Core rather than the ORM because providers reflect
whatever tables already exist. A deployment pointing at an established database
can delete these declarations entirely; they are here so ``crm setup`` can
create a working database from nothing.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    false,
    func,
    text,
)

metadata = MetaData()

#: Instants are stored timezone-aware, in UTC.
#:
#: Without ``timezone=True`` a column records a wall-clock reading with nothing
#: saying which clock, so the same row means different moments depending on
#: where the server happens to be. PostgreSQL honours this as timestamptz;
#: SQLite keeps the offset in the text it stores.
Instant = DateTime(timezone=True)


def timestamps(*extra: Column) -> list[Column]:
    """The columns every table carries, so modules declare them the same way."""
    return [
        Column("created_at", Instant, server_default=func.now(), nullable=False),
        *extra,
    ]


users = Table(
    "users", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(120), nullable=False),
    Column("email", String(160), nullable=False, unique=True, index=True),
    Column("password_hash", String(255), nullable=False),
    # Stored as a JSON array; MultiSelectField handles the conversion.
    Column("roles", String(255), default="[]"),
    Column("is_active", Boolean, default=True, nullable=False),
    # What zone this person reads times in. Instants are stored in UTC and
    # converted for display, so this affects presentation only -- never data.
    Column("timezone", String(60), default="UTC"),
    Column("locale", String(10), default="en"),
    Column("last_login", Instant),
    # -- password state -----------------------------------------------------
    # Only meaningful when a provider that holds passwords is configured. An
    # SSO-only deployment leaves these null, which is the honest representation
    # of "there is no password here".
    Column("password_changed_at", Instant),
    #: Set by an administrator reset. Forces a change at the next sign-in, so a
    #: temporary password cannot quietly become a permanent one.
    # server_default as well as default: a NOT NULL column added to a table
    # that already has rows needs a value the *database* can supply, and a
    # Python-side default is not one.
    Column("must_change_password", Boolean, default=False, server_default=false(),
           nullable=False),
    # Lockout counters live in the row, not in memory: an in-process counter is
    # per-worker, so four workers would give an attacker four times the guesses.
    Column("failed_logins", Integer, default=0, server_default=text("0"), nullable=False),
    Column("last_failed_login", Instant),
    Column("locked_until", Instant),
    *timestamps(),
)

api_tokens = Table(
    "api_tokens", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(120), nullable=False),
    # Only the hash. The token itself is shown once, at creation.
    Column("token_hash", String(64), nullable=False, unique=True, index=True),
    Column("roles", String(255), default="[]"),
    Column("is_active", Boolean, default=True, nullable=False),
    Column("meta", Text),
    # When it stops working. Null means never, which stays an explicit choice
    # -- a token that outlives the integration it was made for is a credential
    # nobody remembers to revoke, and the only guard against that is a date
    # somebody had to decide not to set.
    Column("expires_at", Instant, index=True),
    # Where it was last used, as well as when. Two columns rather than one,
    # because "this token was used at 3am" is only alarming once you know it
    # came from an address the integration has never used.
    Column("last_used", Instant),
    Column("last_used_ip", String(64)),
    # Set when a token's value is replaced. The row survives a rotation --
    # its label, its roles and its history are the reason to keep it -- so
    # without this there is nothing to say the secret itself changed.
    Column("rotated_at", Instant),
    *timestamps(),
)


#: What people have said about a record, as opposed to what they did to it.
#:
#: The audit log already answers "what changed"; this answers "why", which no
#: diff can. One row is one note against one record, addressed the way a
#: notification is -- by resource name and record id as strings -- so a note
#: can hang off a record this application does not own and cannot write to.
timeline_entries = Table(
    "timeline_entries", metadata,
    Column("id", Integer, primary_key=True),
    Column("resource", String(60), nullable=False, index=True),
    Column("record_id", String(60), nullable=False, index=True),
    Column("kind", String(20), default="note"),
    Column("body", Text),
    # Who wrote it, twice over: the name to show, and the identifier to match
    # on. A display name changes and is not unique, so "may I delete this?"
    # and "who else is following this record?" both have to ask the second.
    Column("author", String(120)),
    Column("author_id", String(160), index=True),
    # Whom the note names, as a JSON array of recipient identifiers. Kept on
    # the row rather than re-parsed from the body, so a rename of a person
    # cannot silently change who was told at the time.
    Column("mentions", Text),
    Column("edited_at", Instant),
    # Files attached to the note, as a JSON array of the same records a file
    # column holds -- key, name, size, type -- plus the store that has them.
    # A list rather than a column per file, because a note carries however many
    # somebody dragged onto it; the store name travels with each one so a link
    # written today still resolves after the default store changes.
    Column("attachments", Text),
    # Kept at the top of the panel. The one piece of state a note carries:
    # "read this before you do anything with this record".
    Column("pinned", Boolean, default=False, server_default=false(), nullable=False),
    *timestamps(),
    # The panel's only query -- this record's notes, newest first -- and the
    # reason it is composite: filtering by resource alone on a table holding
    # every record's notes is a scan of the lot.
    Index("ix_timeline_record", "resource", "record_id", "created_at"),
)

# -- access control ---------------------------------------------------------
#
# Roles and their grants live in the database rather than in code, so an
# administrator can change who may do what without a deployment. The Python
# `RolePolicy` remains available for rules that are genuinely structural.

roles = Table(
    "roles", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(60), nullable=False, unique=True, index=True),
    Column("label", String(120)),
    Column("description", Text),
    # A built-in role may be granted but not deleted, so a misclick cannot
    # lock everyone out of the administration screens.
    Column("is_builtin", Boolean, default=False, nullable=False),
    *timestamps(),
)

permissions = Table(
    "permissions", metadata,
    Column("id", Integer, primary_key=True),
    Column("role", String(60), nullable=False, index=True),
    # "*" means every resource, which is how an administrator role is expressed.
    Column("resource", String(60), nullable=False, index=True),
    Column("can_read", Boolean, default=True, nullable=False),
    Column("can_create", Boolean, default=False, nullable=False),
    Column("can_update", Boolean, default=False, nullable=False),
    Column("can_delete", Boolean, default=False, nullable=False),
    # How much of the resource this role sees: every row, or only their own.
    Column("row_scope", String(20), default="all", nullable=False),
    # Optional per-field restrictions, stored as JSON arrays of field names.
    Column("hidden_fields", Text),
    Column("readonly_fields", Text),
    *timestamps(),
)

#: Something a person should know about.
#:
#: Delivery is separate from the notification itself: one row is created, and
#: each channel that handles it records its own outcome. That way a failed
#: email does not lose the in-app notification the user has already seen.
notifications = Table(
    "notifications", metadata,
    Column("id", Integer, primary_key=True),
    Column("created_at", Instant, server_default=func.now(), nullable=False, index=True),
    # Who it is for, as the identifier every auth provider supplies.
    Column("recipient", String(160), nullable=False, index=True),
    Column("kind", String(40), default="info", index=True),
    Column("title", String(200), nullable=False),
    Column("body", Text),
    # What it is about, so the notification can link back to it.
    Column("resource", String(60), index=True),
    Column("record_id", String(60)),
    Column("url", String(400)),
    Column("read_at", Instant, index=True),
    # When it should be delivered. A reminder is a notification with a future
    # date, which is why this is not simply "now".
    Column("due_at", Instant, index=True),
    Column("sent_at", Instant),
    Column("channels", String(200)),
    Column("delivery", Text),
    Column("actor", String(160)),
    Column("priority", String(20), default="normal", index=True),
)

#: Work for a person, as opposed to work for a worker.
#:
#: The ``jobs`` table below is the machine's queue; this is the back office's.
#: They are deliberately separate: a job is retried, claimed and abandoned on a
#: timeout, and none of those verbs mean anything for something a colleague has
#: promised to do.
#:
#: A task addresses the record it concerns the way a note does -- resource name
#: and record id as strings -- so a task can hang off a company this
#: application only reads.
tasks = Table(
    "tasks", metadata,
    Column("id", Integer, primary_key=True),
    Column("title", String(200), nullable=False),
    Column("body", Text),
    Column("resource", String(60), index=True),
    Column("record_id", String(60)),
    # Who is doing it, as the identifier notifications are addressed to, plus
    # the name to show. Assignment is a deliberate act by a person: nothing
    # here assigns a task automatically, because a queue that assigns itself is
    # a queue nobody feels responsible for.
    Column("assignee", String(160), index=True),
    Column("assignee_name", String(160)),
    # open -> doing -> done, with blocked and cancelled as the two ways out.
    Column("state", String(20), nullable=False, default="open", server_default="open",
           index=True),
    Column("priority", String(20), default="normal", index=True),
    Column("due_at", Instant, index=True),
    Column("created_by", String(160)),
    Column("created_by_name", String(160)),
    Column("done_at", Instant),
    Column("done_by", String(160)),
    # The monitor's bookkeeping. `notified_assignee` is who was last told this
    # task is theirs: comparing it with `assignee` is what makes a hand-over
    # noticed no matter which screen made it, and what stops the same hand-over
    # being announced on every sweep. `reminded_at` does the same for the due
    # date.
    Column("notified_assignee", String(160)),
    Column("reminded_at", Instant),
    # The other system's key for the condition this task is about, when one
    # raised it: what makes delivering the same event twice harmless.
    Column("source_key", String(160)),
    *timestamps(),
    # The sweep's query -- open tasks, by when they are due -- and the list's
    # default sort.
    Index("ix_tasks_open", "state", "due_at"),
    Index(
        "uq_tasks_open_source_key", "source_key", unique=True,
        postgresql_where=text("source_key IS NOT NULL AND state IN ('open', 'doing', 'blocked')"),
        sqlite_where=text("source_key IS NOT NULL AND state IN ('open', 'doing', 'blocked')"),
    ),
)

#: Background work that must survive the worker that raised it.
#:
#: A row here is a promise: something has been accepted and will be done, by
#: this process or another, now or after a restart. That is the whole reason
#: the table exists -- an ``asyncio`` task is faster and is lost when the
#: worker stops, which is fine for a delivery nobody is waiting on and not fine
#: for one somebody was told had been accepted.
jobs = Table(
    "jobs", metadata,
    Column("id", Integer, primary_key=True),
    Column("created_at", Instant, server_default=func.now(), nullable=False),
    # What to run, and what to run it with. The kind names a registered
    # handler; a job whose kind nothing handles is parked rather than lost, so
    # deploying the handler later still runs it.
    Column("kind", String(60), nullable=False, index=True),
    Column("payload", Text),
    # queued -> running -> done | failed. `queued` with a future run_at is a
    # delayed job; `queued` with attempts > 0 is a retry waiting its turn.
    Column("status", String(20), nullable=False, default="queued", index=True),
    # When it becomes eligible. Claiming filters on this, so a retry backoff is
    # just a later value rather than a sleeping task somewhere.
    Column("run_at", Instant, nullable=False, index=True),
    Column("attempts", Integer, nullable=False, default=0),
    Column("max_attempts", Integer, nullable=False, default=5),
    # Which worker holds it, and since when. Together they are how a job
    # abandoned by a killed worker is found and released.
    Column("claimed_by", String(80), index=True),
    Column("claimed_at", Instant),
    Column("finished_at", Instant),
    Column("last_error", Text),
    # Optional idempotency handle, so a caller that cannot avoid enqueuing
    # twice can at least say the two are the same job.
    Column("key", String(200), index=True),
    Column("priority", Integer, nullable=False, default=0, index=True),
    # The claim query -- "queued, and due" -- runs once per worker per poll,
    # which is the most frequent query in the application by some margin. A
    # composite index answers it from one scan; two single-column indexes make
    # the database choose one and filter the rest.
    Index("ix_jobs_claim", "status", "run_at"),
)

#: What happened, who did it, and what changed.
audit_log = Table(
    "audit_log", metadata,
    Column("id", Integer, primary_key=True),
    Column("at", Instant, server_default=func.now(), nullable=False, index=True),
    Column("actor", String(160), index=True),
    Column("actor_name", String(160)),
    Column("actor_provider", String(40)),
    Column("action", String(20), nullable=False, index=True),
    Column("resource", String(60), nullable=False, index=True),
    Column("record_id", String(60), index=True),
    Column("record_label", String(200)),
    # Only the fields that actually changed, as {field: [before, after]}.
    Column("changes", Text),
    Column("status", String(20), default="ok"),
    Column("detail", Text),
    Column("request_id", String(40)),
    Column("ip", String(64)),
)

#: A support ticket: a request from outside the organisation, or one a staff
#: member raised on somebody's behalf, tracked from receipt through to close.
#:
#: ``reference`` is what a ticket is quoted by -- a subject tag, a sentence
#: read over the phone -- because a primary key is not a thing anyone says out
#: loud with confidence they will be understood. It is filled in by
#: :class:`app.providers.sequence.SequencingProvider` immediately after the
#: insert that reveals the primary key it is derived from.
#:
#: ``notified_assignee`` and ``reminded_at`` mirror ``tasks``, for the same
#: reason: ``crm helpdesk-sweep`` announces a hand-over and a ticket ageing
#: past a threshold by comparing columns rather than hooking the write, so
#: every creation and assignment path -- the form, an inline edit, the ingest
#: branch this table is already shaped for -- is covered by one sweep instead
#: of a notification remembered at each of them.
#:
#: ``thread_message_id`` is the anchor a reply threads onto: the Message-ID of
#: whichever message, inbound or outbound, was added to the ticket most
#: recently, so the next outbound reply's In-Reply-To and References point at
#: what was actually said last rather than at the first message forever.
tickets = Table(
    "tickets", metadata,
    Column("id", Integer, primary_key=True),
    Column("reference", String(20), index=True),
    Column("subject", String(200), nullable=False),
    Column("requester_name", String(160)),
    Column("requester_email", String(160), nullable=False, index=True),
    Column("state", String(20), nullable=False, default="new", server_default="new",
           index=True),
    Column("priority", String(20), default="normal", index=True),
    # Who is on it, as the identifier notifications are addressed to, plus the
    # name to show -- the same shape as `tasks.assignee`. Assignment is manual
    # here too: nothing here picks an assignee.
    Column("assignee", String(160), index=True),
    Column("assignee_name", String(160)),
    # "staff" is the only value this branch ever writes; the ingest branch
    # writes "email". The column exists now so that branch's migration is
    # adding rows, not altering a table that already has live ones.
    Column("source", String(20), nullable=False, default="staff", server_default="staff",
           index=True),
    Column("thread_message_id", String(255)),
    Column("created_by", String(160)),
    Column("created_by_name", String(160)),
    Column("notified_assignee", String(160)),
    Column("reminded_at", Instant),
    *timestamps(),
    # The board's grouping query and the sweep's only query: open tickets,
    # oldest first.
    Index("ix_tickets_open", "state", "created_at"),
)

#: What was said to, or received from, the requester -- and only that.
#:
#: An internal remark ("waiting on billing to confirm the refund") belongs in
#: ``timeline_entries``, the activity panel every other record already gets,
#: not here. That is the separation that matters most about this table: there
#: is nothing in its shape -- no flag, no visibility column -- that a private
#: remark could be filed under by mistake. A row here *is*, by definition,
#: something that was or can be emailed to the customer; the only way to keep
#: a remark private is to write it somewhere else, which is exactly what the
#: notes panel is for.
#:
#: ``message_id`` carries a unique constraint now, on a table this branch
#: never writes a duplicate into, because the ingest branch dedupes an
#: at-least-once mail fetch against it and a constraint added after that
#: branch has real rows would be a migration that can fail on the data it
#: exists to protect.
ticket_messages = Table(
    "ticket_messages", metadata,
    Column("id", Integer, primary_key=True),
    Column("ticket_id", Integer, ForeignKey("tickets.id"), nullable=False, index=True),
    Column("direction", String(10), nullable=False, index=True),
    Column("author", String(160)),
    Column("body", Text, nullable=False),
    Column("message_id", String(255), unique=True),
    Column("in_reply_to", String(255)),
    Column("references", Text),
    Column("sent_at", Instant),
    Column("received_at", Instant),
    *timestamps(),
    # The thread's only query: one ticket's messages, in the order they were
    # written.
    Index("ix_ticket_messages_thread", "ticket_id", "created_at"),
)

#: The tables that ship regardless of which modules are enabled.
#:
#: Not the same as ``metadata.tables``, which also holds whatever the loaded
#: modules declared -- that is the set ``create_all`` and Alembic work from.
PLATFORM_TABLES = (
    users, api_tokens, roles, permissions, audit_log, notifications,
    timeline_entries, tasks, jobs, tickets, ticket_messages,
)
