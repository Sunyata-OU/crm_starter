"""Sample deals and activities.

Runs after ``demo_crm``'s seeder -- module order is dependency order -- so the
company and contact ids referenced below exist by the time these rows land.
"""

from __future__ import annotations

from datetime import timedelta

from app.seed import SeedContext

from .schema import activities, deals

DEAL_ROWS = [
    ("Analytical platform renewal", 1,  1, 48000, "won",         100, -20, "referral"),
    ("Difference engine upgrade",   1,  2, 15500, "proposal",     40,  12, "inbound"),
    ("Bletchley archive migration", 2,  3, 92000, "negotiation",  70,   8, "outbound"),
    ("Cryptography training",       2,  4,  8200, "qualifying",   20,  30, "inbound"),
    ("Naval logistics rollout",     3,  5, 210000,"negotiation",  65,  21, "partner"),
    ("Compiler support contract",   3,  5, 34000, "won",         100, -45, "referral"),
    ("Network redesign",            4,  6, 27500, "proposal",     50,  14, "outbound"),
    ("Guidance software audit",     5,  7, 76000, "qualifying",   25,  45, "inbound"),
    ("Risk modelling suite",        6,  8, 145000,"lost",          0, -10, "outbound"),
    ("Trajectory analysis tooling", 7,  9, 58000, "proposal",     45,  18, "referral"),
    ("Clinical records pilot",      8, 10, 12000, "qualifying",   15,  60, "inbound"),
    ("Retail forecasting",          9, 11, 88000, "negotiation",  60,   5, "partner"),
    ("Faculty licence renewal",    10, 12,  9500, "lost",          0, -30, "inbound"),
    ("Storage expansion",           3,  5, 41000, "qualifying",   10,  75, "outbound"),
    ("Support tier upgrade",        1,  1, 19000, "won",         100,  -5, "inbound"),
]

ACTIVITY_ROWS = [
    ("Quarterly review call",       "call",     1,  1,  -3, 45, True),
    ("Send revised proposal",       "email",    2,  2,   1, None, False),
    ("Archive migration workshop",  "meeting",  3,  3,   2, 120, False),
    ("Follow up on training scope", "task",     4,  4,   4, None, False),
    ("Logistics kickoff",           "meeting",  5,  5,   0, 90, False),
    ("Renewal paperwork",           "task",     6,  5,  -8, 30, True),
    ("Network design review",       "meeting",  7,  6,   6, 60, False),
    ("Audit scoping call",          "call",     8,  7,   9, 30, False),
    ("Post-mortem on lost bid",     "meeting",  9,  8,  -6, 60, True),
    ("Share trajectory demo",       "email",   10,  9,   3, None, False),
    ("Pilot check-in",              "call",    11, 10,   7, 30, False),
    ("Forecasting data handover",   "task",    12, 11,   1, None, False),
    ("Contract renewal reminder",   "task",    13, 12,  11, None, False),
    ("Capacity planning session",   "meeting", 14,  5,  14, 90, False),
    ("Upgrade confirmation",        "email",   15,  1,  -2, None, True),
]


async def seed(ctx: SeedContext) -> dict[str, int]:
    today = ctx.today

    await ctx.conn.execute(
        deals.insert(),
        [
            {
                "name": name, "company_id": company_id, "contact_id": contact_id,
                "amount": amount, "stage": stage, "probability": probability,
                "expected_close": today + timedelta(days=offset),
                "closed_on": (today + timedelta(days=offset)) if stage in ("won", "lost") else None,
                "owner": ctx.owner(i),
                "source": source,
                "notes": None if i % 3 else "Budget confirmed by finance.",
            }
            for i, (name, company_id, contact_id, amount, stage, probability, offset, source)
            in enumerate(DEAL_ROWS)
        ],
    )

    await ctx.conn.execute(
        activities.insert(),
        [
            {
                "subject": subject, "kind": kind, "deal_id": deal_id,
                "contact_id": contact_id,
                "due_on": today + timedelta(days=offset),
                "minutes": minutes, "done": done,
                "owner": ctx.owner(i),
                "notes": None if i % 2 else "Agenda circulated beforehand.",
            }
            for i, (subject, kind, deal_id, contact_id, offset, minutes, done)
            in enumerate(ACTIVITY_ROWS)
        ],
    )

    return {"deals": len(DEAL_ROWS), "activities": len(ACTIVITY_ROWS)}
