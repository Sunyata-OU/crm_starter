"""Fixtures for the web and auth suites.

The application is built against in-memory providers rather than a database, so
the tests exercise the real routes, templates, policies and form engine while
staying fast enough to run on every save.
"""

from __future__ import annotations

from datetime import date

import pytest
from starlette.testclient import TestClient

from app.auth.local import hash_password
from app.core.registry import Registry
from app.fields.types import (
    BooleanField,
    CurrencyField,
    DateField,
    EmailField,
    RelationField,
    SelectField,
    StatusField,
    TextAreaField,
    TextField,
)
from app.main import create_app
from app.providers.memory import MemoryProvider
from app.resources.policy import OwnerPolicy, RolePolicy
from app.resources.resource import Resource
from app.resources.views import (
    BoardView,
    Card,
    ChartView,
    Column,
    FormView,
    ListView,
    SearchSpec,
    Section,
)
from app.settings import Settings

TODAY = date(2026, 6, 15)

#: A small, deliberately awkward dataset: mixed stages, several owners, and
#: nulls in two columns so "missing value" handling is always exercised.
SAMPLE_ROWS: list[dict] = [
    {"id": 1, "name": "Ada Lovelace",   "email": "ada@analytical.io",   "stage": "won",  "amount": 5000, "owner": "kim",  "closed": date(2026, 1, 5),  "note": None},
    {"id": 2, "name": "Grace Hopper",   "email": "grace@navy.mil",      "stage": "won",  "amount": 8200, "owner": "kim",  "closed": date(2026, 2, 11), "note": "referral"},
    {"id": 3, "name": "Alan Turing",    "email": "alan@bletchley.uk",   "stage": "open", "amount": 3100, "owner": "sam",  "closed": None,             "note": "inbound"},
    {"id": 4, "name": "Katherine J.",   "email": "kj@nasa.gov",         "stage": "open", "amount": 9900, "owner": "sam",  "closed": None,             "note": None},
    {"id": 5, "name": "Barbara Liskov", "email": "barbara@mit.edu",     "stage": "lost", "amount": 1200, "owner": "kim",  "closed": date(2026, 3, 2),  "note": "budget"},
    {"id": 6, "name": "Radia Perlman",  "email": "radia@spanning.net",  "stage": "open", "amount": 7300, "owner": "lee",  "closed": None,             "note": "spanning tree"},
    {"id": 7, "name": "Margaret H.",    "email": "mh@apollo.gov",       "stage": "won",  "amount": 6400, "owner": "lee",  "closed": date(2026, 1, 30), "note": None},
]

SEARCH_FIELDS = ("name", "email", "note")


@pytest.fixture
def rows() -> list[dict]:
    """A fresh copy of the sample rows, so a test may mutate them freely."""
    return [dict(r) for r in SAMPLE_ROWS]


COMPANY_ROWS = [
    {"id": 1, "name": "Analytical Engines", "industry": "Software"},
    {"id": 2, "name": "Bletchley Systems", "industry": "Public sector"},
]

CONTACT_ROWS = [
    {"id": 1, "name": "Ada Lovelace", "email": "ada@analytical.test",
     "company_id": 1, "status": "active", "owner": "kim@example.com", "subscribed": True},
    {"id": 2, "name": "Alan Turing", "email": "alan@bletchley.test",
     "company_id": 2, "status": "lead", "owner": "sam@example.com", "subscribed": False},
    {"id": 3, "name": "Grace Hopper", "email": "grace@navy.test",
     "company_id": None, "status": "active", "owner": "kim@example.com", "subscribed": True},
]

DEAL_ROWS = [
    {"id": 1, "name": "Platform renewal", "company_id": 1, "amount": 5000,
     "stage": "won", "owner": "kim@example.com", "closed_on": TODAY},
    {"id": 2, "name": "Archive migration", "company_id": 2, "amount": 9000,
     "stage": "open", "owner": "sam@example.com", "closed_on": None},
    {"id": 3, "name": "Support contract", "company_id": 1, "amount": 2500,
     "stage": "open", "owner": "kim@example.com", "closed_on": None},
]

#: Every test account uses this password, so the real login flow can be
#: exercised rather than only the cookie shortcut.
TEST_PASSWORD = "test-password"

USER_ROWS = [
    {"id": 1, "name": "Admin", "email": "admin@example.com",
     "password_hash": hash_password(TEST_PASSWORD), "roles": ["admin"], "is_active": True},
    {"id": 2, "name": "Kim", "email": "kim@example.com",
     "password_hash": hash_password(TEST_PASSWORD), "roles": ["user"], "is_active": True},
]

STAGES = [("open", "Open", "amber"), ("won", "Won", "green"), ("lost", "Lost", "red")]
STATUSES = [("lead", "Lead", "amber"), ("active", "Active", "green")]


def build_registry() -> Registry:
    """A registry of in-memory resources exercising every view type."""
    registry = Registry()

    registry.add_resource(
        Resource(
            "companies",
            provider=MemoryProvider(COMPANY_ROWS, searchable_fields=("name",)),
            fields=[
                TextField("id", in_form=False),
                TextField("name", required=True, searchable=True, inline_editable=True),
                SelectField("industry", choices=["Software", "Public sector"], in_filter=True),
            ],
        )
    )

    registry.add_resource(
        Resource(
            "contacts",
            provider=MemoryProvider(CONTACT_ROWS, searchable_fields=("name", "email")),
            fields=[
                TextField("id", in_form=False),
                TextField("name", required=True, searchable=True, inline_editable=True),
                EmailField("email", required=True, searchable=True, inline_editable=True),
                RelationField("company_id", label="Company", resource="companies",
                              display="name", in_filter=True),
                StatusField("status", choices=STATUSES, default="lead", in_filter=True,
                            inline_editable=True),
                BooleanField("subscribed", default=True, inline_editable=True, in_filter=True),
                TextField("owner", in_filter=True),
                TextAreaField("notes"),
            ],
            search=SearchSpec(fields=("name", "email"), filters=("status", "company_id")),
            views=[
                ListView(
                    columns=[Column("name", link=True), "email", "company_id", "status"],
                    bulk_actions=["delete"],
                ),
                FormView([
                    Section("Person", ["name", "email"], columns=2),
                    Section("Links", ["company_id", "status", "owner", "subscribed"], columns=2),
                    Section("Notes", ["notes"], columns=1),
                ]),
            ],
        )
    )

    registry.add_resource(
        Resource(
            "deals",
            provider=MemoryProvider(DEAL_ROWS, searchable_fields=("name",)),
            # The row-scoping case: a rep sees only their own.
            policy=OwnerPolicy("owner", identity_attr="email",
                               bypass_roles=("admin", "manager")),
            fields=[
                TextField("id", in_form=False),
                TextField("name", required=True, searchable=True, inline_editable=True),
                RelationField("company_id", label="Company", resource="companies", display="name"),
                CurrencyField("amount", inline_editable=True),
                StatusField("stage", choices=STAGES, default="open", in_filter=True,
                            inline_editable=True),
                DateField("closed_on", readonly=True, in_form=False),
                TextField("owner", in_filter=True),
            ],
            views=[
                ListView(columns=[Column("name", link=True), "amount", "stage"],
                         bulk_actions=["delete"]),
                BoardView(group_by="stage", card=Card(title="name", badges=["amount"]),
                          sum_field="amount"),
                ChartView(group_by="stage"),
                FormView([Section("Deal", ["name", "company_id", "amount", "stage", "owner"],
                                  columns=2)]),
            ],
        )
    )

    registry.add_resource(
        Resource(
            "users",
            provider=MemoryProvider(USER_ROWS, searchable_fields=("name", "email")),
            policy=RolePolicy(read=["admin"], write=["admin"]),
            fields=[
                TextField("id", in_form=False),
                TextField("name", required=True),
                EmailField("email", required=True),
                # Never rendered anywhere.
                TextField("password_hash", read_roles=["nobody"], in_list=False,
                          in_form=False, in_detail=False),
            ],
        )
    )
    return registry


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        secret_key="test-key-not-for-real-use",
        debug=False,
        template_reload=False,
        csrf_enabled=True,
        auth_providers=["session", "local"],
        modules=[],
    )


@pytest.fixture
def app(settings):
    return create_app(settings=settings, registry=build_registry())


@pytest.fixture
def client(app):
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def sign_in(
    client: TestClient,
    *,
    email: str,
    roles: list[str],
    subject: str = "",
    timezone: str = "UTC",
) -> None:
    """Put a signed session cookie in place, bypassing the login form.

    The login form has its own tests; everything else needs an identity, not a
    re-test of how one is obtained.

    ``timezone`` is the signed-in person's own preference, which is what every
    rendered instant is converted into. It takes a parameter because "the same
    row read from Warsaw and from UTC" is a difference several screens are
    supposed to show, and a test cannot ask for it any other way.
    """
    from starlette.responses import Response

    from app.core.results import Identity

    state = client.app.state.crm
    identity = Identity(
        subject=subject or email,
        email=email,
        display_name=email.split("@")[0].title(),
        roles=frozenset(roles),
        provider="session",
        timezone=timezone,
    )
    carrier = Response()
    state.sessions.save_identity(carrier, identity)
    cookie = carrier.headers["set-cookie"].split(";")[0]
    name, _, value = cookie.partition("=")
    client.cookies.set(name, value)


@pytest.fixture
def admin(client):
    sign_in(client, email="admin@example.com", roles=["admin"])
    return client


@pytest.fixture
def rep(client):
    """A user who owns some deals but not others."""
    sign_in(client, email="kim@example.com", roles=["user"])
    return client


def csrf_from(client: TestClient, url: str) -> str:
    """Pull a CSRF token out of a rendered form."""
    import re

    html = client.get(url).text
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, f"no CSRF token found in {url}"
    return match.group(1)
