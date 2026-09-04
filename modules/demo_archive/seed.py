"""Sample archived deals.

Only runs against the database its table lives in. ``crm seed`` skips a
module whose tables are not on the connection being seeded, which is why this
file may assume ``archived_deals`` exists without checking.
"""

from __future__ import annotations

from datetime import timedelta

from app.seed import SeedContext

from .schema import archived_deals

#: name, company, amount, stage, days before today it closed
ARCHIVED_ROWS = [
    ("Punched card reader supply",  "Analytical Engines",  22000, "won",  400),
    ("Bombe maintenance contract",  "Bletchley Systems",   61000, "won",  512),
    ("Naval cipher consultancy",    "Navy Systems",        18500, "lost", 620),
    ("Mark I installation",         "Harvard Compute",     97000, "won",  735),
    ("Relay replacement programme", "Harvard Compute",     14200, "lost", 810),
    ("Spanning tree rollout",       "Spanning Networks",   45000, "won",  902),
    ("Apollo guidance review",      "Apollo Systems",     130000, "won", 1024),
]


async def seed(ctx: SeedContext) -> dict[str, int]:
    rows = [
        {
            "name": name,
            "company_name": company,
            "amount": amount,
            "stage": stage,
            "closed_on": ctx.today - timedelta(days=days),
            "expected_close": ctx.today - timedelta(days=days + 14),
            "owner": ctx.owner(index),
            "origin": ("referral", "inbound", "outbound", "partner")[index % 4],
            "notes": None,
        }
        for index, (name, company, amount, stage, days) in enumerate(ARCHIVED_ROWS)
    ]
    await ctx.conn.execute(archived_deals.insert(), rows)
    return {"archived deals": len(rows)}
