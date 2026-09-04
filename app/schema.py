"""The platform schema: accounts, access control, audit and notifications.

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
    Column("last_used", Instant),
    *timestamps(),
)


#: Written by the timeline when a record changes.
timeline_entries = Table(
    "timeline_entries", metadata,
    Column("id", Integer, primary_key=True),
    Column("resource", String(60), nullable=False, index=True),
    Column("record_id", String(60), nullable=False, index=True),
    Column("kind", String(20), default="note"),
    Column("body", Text),
    Column("author", String(120)),
    *timestamps(),
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

#: The tables that ship regardless of which modules are enabled.
#:
#: Not the same as ``metadata.tables``, which also holds whatever the loaded
#: modules declared -- that is the set ``create_all`` and Alembic work from.
PLATFORM_TABLES = (
    users, api_tokens, roles, permissions, audit_log, notifications,
    timeline_entries, jobs,
)
