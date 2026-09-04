"""Sample data.

Two halves, matching the way the application is put together. The platform
seeds what it owns -- roles, grants and a handful of accounts -- and then each
enabled module seeds its own tables through the hook below. So ``crm seed``
fills exactly the database the enabled modules describe, and a deployment that
has deleted the demo modules still gets a working sign-in.

The demo rows are shaped to exercise the views rather than to look impressive:
a spread of stages so the board has content in every column, activities across
the current month so the calendar is not empty, deliberate nulls so the
"missing value" rendering is visible, and several owners so row scoping can be
seen working.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.auth.local import hash_password
from app.core.clock import utcnow
from app.core.modules import LoadedModule, submodule
from app.resources.rbac import DEFAULT_GRANTS, DEFAULT_ROLES
from app.schema import metadata, permissions, roles, users

#: Owner columns hold the account's email address. It is the one identifier
#: every auth provider supplies -- a local row id is meaningless to SSO, and a
#: first name is not unique -- so scoping works whichever provider signed the
#: user in.
OWNERS = ["kim@example.com", "sam@example.com", "lee@example.com"]

#: Spread across zones on purpose: the same records then read with a different
#: local time for each of them, which is the quickest way to see that the
#: timezone handling is real.
ACCOUNTS = [
    ("Admin User",   "admin@example.com",    ["admin"],    True,  "UTC"),
    ("Morgan Reid",  "manager@example.com",  ["manager"],  True,  "Europe/London"),
    ("Kim Alvarez",  "kim@example.com",      ["user"],     True,  "America/New_York"),
    ("Sam Okafor",   "sam@example.com",      ["user"],     True,  "Africa/Lagos"),
    ("Lee Nakamura", "lee@example.com",      ["user"],     True,  "Asia/Tokyo"),
    ("Dana Reed",    "readonly@example.com", ["readonly"], False, "UTC"),
]


@dataclass(frozen=True, slots=True)
class SeedContext:
    """What a module's seeder is handed.

    The connection is already inside a transaction and the platform tables are
    already populated, so a module may reference the seeded accounts -- that is
    what ``owners`` is for.
    """

    conn: AsyncConnection
    owners: list[str]
    today: date
    #: Seeded to a fixed value, so two runs produce the same database.
    random: random.Random

    def owner(self, index: int) -> str:
        """Spread rows across the demo accounts, in a stable order."""
        return self.owners[index % len(self.owners)]


class Seeder(Protocol):
    """The hook a module exposes to fill its own tables.

    Optional: a module with nothing to demonstrate simply omits it.
    """

    async def __call__(self, ctx: SeedContext) -> dict[str, int]: ...


async def seed(
    engine: AsyncEngine,
    *,
    # Long enough to satisfy the shipped password policy, so the first thing a
    # new user does is not discover that the demo credentials would be rejected
    # by the change-password form.
    password: str = "demo-password",
    reset: bool = True,
    modules: list[LoadedModule] | None = None,
) -> dict[str, Any]:
    """Create the schema and fill it with sample data.

    ``modules`` is the loaded set, in dependency order, so a module's seeder
    runs after the seeders of the modules it depends on -- which is what lets
    ``demo_sales`` reference the companies ``demo_crm`` inserted.

    Returns a summary so the CLI can report what it did.
    """
    counts: dict[str, int] = {}

    async with engine.begin() as conn:
        if reset:
            await conn.run_sync(metadata.drop_all)
        # create_all rather than running the migrations: seeding is for a demo
        # or a test database, and should not depend on migration history being
        # intact. `crm migrate` is what a real deployment runs.
        await conn.run_sync(metadata.create_all)

        # -- roles and grants -------------------------------------------
        # Seeded from the same defaults the RBAC module documents, so the
        # shipped permissions and the code's fallback agree.
        await conn.execute(roles.insert(), [dict(r) for r in DEFAULT_ROLES])
        await conn.execute(permissions.insert(), [dict(g) for g in DEFAULT_GRANTS])
        counts["roles"] = len(DEFAULT_ROLES)
        counts["grants"] = len(DEFAULT_GRANTS)

        # -- accounts ---------------------------------------------------
        hashed = hash_password(password)
        await conn.execute(
            users.insert(),
            [
                {"name": name, "email": email, "password_hash": hashed,
                 "roles": json.dumps(user_roles), "is_active": active,
                 "timezone": tz, "created_at": utcnow()}
                for name, email, user_roles, active, tz in ACCOUNTS
            ],
        )
        counts["users"] = len(ACCOUNTS)

        ctx = SeedContext(
            conn=conn,
            owners=list(OWNERS),
            today=date.today(),
            random=random.Random(20260101),  # stable output between runs
        )
        for module in modules or []:
            # By convention: a module seeds itself from a `seed.py` beside its
            # `__init__.py`. Absent, the module simply contributes no rows.
            source = submodule(module, "seed")
            seeder: Seeder | None = getattr(source, "seed", None) if source else None
            if seeder is None:
                continue
            counts.update(await seeder(ctx))

    return {**counts, "password": password}
