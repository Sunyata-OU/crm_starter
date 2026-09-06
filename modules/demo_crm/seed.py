"""Sample companies and contacts.

Exposed as ``seed`` on the package, which is the hook ``crm seed`` looks for.
A module that ships no demo data simply does not define it.
"""

from __future__ import annotations

import json
from datetime import timedelta

from app.core.clock import utcnow
from app.seed import SeedContext

from .schema import companies, contacts

COMPANY_ROWS = [
    ("Analytical Engines",  "https://analytical.example",   "Software",           "mid", "London",     "United Kingdom"),
    ("Bletchley Systems",   "https://bletchley.example",    "Public sector",      "ent", "Milton Keynes", "United Kingdom"),
    ("Hopper Naval Supply", "https://hopper.example",       "Manufacturing",      "ent", "Arlington",  "United States"),
    ("Spanning Tree Ltd",   "https://spanning.example",     "Software",           "smb", "Cambridge",  "United Kingdom"),
    ("Apollo Guidance Co",  "https://apollo.example",       "Manufacturing",      "mid", "Houston",    "United States"),
    ("Liskov Financial",    "https://liskov.example",       "Financial services", "ent", "Boston",     "United States"),
    ("Johnson Aerospace",   None,                           "Manufacturing",      "mid", "Hampton",    "United States"),
    ("Clarke Medical",      "https://clarke.example",       "Healthcare",         "smb", "Bristol",    "United Kingdom"),
    ("Turing Retail Group", "https://turingretail.example", "Retail",             "ent", "Manchester", "United Kingdom"),
    ("Noether Education",   None,                           "Education",          "smb", "Göttingen",  "Germany"),
]

#: Where each company sits, and which company owns it. Coordinates are the city
#: centres named in COMPANY_ROWS, to four decimal places -- enough to land in
#: the right place on a map and honest about being approximate. The parent is a
#: 1-based index into this same list, which is what makes the tree a self-join.
COMPANY_GEOGRAPHY = [
    # (latitude, longitude, parent index or None)
    (51.5072,   -0.1276, None),   # Analytical Engines, London
    (52.0406,   -0.7594, None),   # Bletchley Systems, Milton Keynes
    (38.8816,  -77.0910, None),   # Hopper Naval Supply, Arlington
    (52.2053,    0.1218, 1),      # Spanning Tree Ltd, Cambridge -- under Analytical
    (29.7604,  -95.3698, 3),      # Apollo Guidance Co, Houston -- under Hopper
    (42.3601,  -71.0589, None),   # Liskov Financial, Boston
    (37.0299,  -76.3452, 3),      # Johnson Aerospace, Hampton -- under Hopper
    (51.4545,   -2.5879, 1),      # Clarke Medical, Bristol -- under Analytical
    (53.4808,   -2.2426, None),   # Turing Retail Group, Manchester
    (None,        None,  9),      # Noether Education -- under Turing Retail, unplaced
]

CONTACT_ROWS = [
    ("Ada Lovelace",     "ada@analytical.example",   "+44 20 7946 0001", "Chief Analyst",       1, "active"),
    ("Charles Babbage",  "charles@analytical.example", None,             "Founder",             1, "active"),
    ("Alan Turing",      "alan@bletchley.example",   "+44 1908 640001",  "Head of Research",    2, "lead"),
    ("Joan Clarke",      "joan@bletchley.example",   None,               "Cryptanalyst",        2, "active"),
    ("Grace Hopper",     "grace@hopper.example",     "+1 703 555 0102",  "Director of Systems", 3, "active"),
    ("Radia Perlman",    "radia@spanning.example",   "+44 1223 555 010", "Principal Engineer",  4, "lead"),
    ("Margaret Hamilton","margaret@apollo.example",  "+1 281 555 0143",  "Director of Software",5, "active"),
    ("Barbara Liskov",   "barbara@liskov.example",   None,               "CTO",                 6, "dormant"),
    ("Katherine Johnson","katherine@johnson.example","+1 757 555 0177",  "Lead Mathematician",  7, "active"),
    ("Arthur Clarke",    "arthur@clarke.example",    None,               "Medical Director",    8, "lead"),
    ("Sophie Wilson",    "sophie@turingretail.example", "+44 161 555 01", "Chief Architect",    9, "active"),
    ("Emmy Noether",     "emmy@noether.example",     None,               "Head of Faculty",    10, "churned"),
]


async def seed(ctx: SeedContext) -> dict[str, int]:
    now = utcnow()

    await ctx.conn.execute(
        companies.insert(),
        [
            {
                "name": name, "website": website, "industry": industry,
                "size": size, "city": city, "country": country,
                "owner": ctx.owner(i),
                "notes": None if i % 3 else f"Key account since {2019 + i % 5}.",
                "parent_id": COMPANY_GEOGRAPHY[i][2],
                "latitude": COMPANY_GEOGRAPHY[i][0],
                "longitude": COMPANY_GEOGRAPHY[i][1],
            }
            for i, (name, website, industry, size, city, country) in enumerate(COMPANY_ROWS)
        ],
    )

    await ctx.conn.execute(
        contacts.insert(),
        [
            {
                "name": name, "email": email, "phone": phone, "title": title,
                "company_id": company_id, "status": status,
                "tags": json.dumps(["vip"] if i % 4 == 0 else (["technical"] if i % 3 == 0 else [])),
                "owner": ctx.owner(i),
                "subscribed": i % 5 != 0,
                "notes": None if i % 2 else "Met at the spring conference.",
                "last_contacted": now - timedelta(days=ctx.random.randint(1, 90)),
            }
            for i, (name, email, phone, title, company_id, status) in enumerate(CONTACT_ROWS)
        ],
    )

    return {"companies": len(COMPANY_ROWS), "contacts": len(CONTACT_ROWS)}
