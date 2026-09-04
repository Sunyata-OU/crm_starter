"""A worked example: companies and the people who work at them.

Demo only. Nothing in the platform depends on this module, and deleting the
directory is a supported way to start a real application -- which is the point
of it being a module at all. Enable it with
``CRM_MODULES=core_identity,core_access,demo_crm``.

Read it as the tutorial. Everything here is ordinary declaration -- no routes,
no templates, no queries -- and it produces a complete set of screens: list,
form, detail with a timeline, filters, search, inline editing, CSV export and
a JSON API.

The two tables it needs are declared in ``schema.py`` next to it, against the
metadata the platform owns, so a deployment that enables this module gets its
tables from the same migration run as everything else.
"""

from __future__ import annotations

from app.core.registry import Registry
from app.fields.types import (
    BackrefField,
    BooleanField,
    DateTimeField,
    EmailField,
    FileField,
    ImageField,
    MultiSelectField,
    PhoneField,
    RelationField,
    SelectField,
    StatusField,
    TextAreaField,
    TextField,
    URLField,
)
from app.resources.rbac import DbPolicy
from app.resources.resource import Resource
from app.resources.views import (
    Column,
    DetailView,
    FormView,
    ListView,
    SearchSpec,
    Section,
)

MANIFEST = {
    "name": "demo_crm",
    "label": "Demo: companies and contacts",
    "description": "A worked example. Not loaded unless it is enabled.",
    "depends": ("core_identity",),
    "menu_groups": {"Records": 10},
    # Nothing ships by default beyond the platform itself.
    "optional": True,
}

COMPANY_SIZES = [
    ("smb", "1–50"),
    ("mid", "51–500"),
    ("ent", "500+"),
]

INDUSTRIES = [
    "Software", "Financial services", "Healthcare", "Manufacturing",
    "Retail", "Education", "Public sector", "Other",
]

CONTACT_STATUSES = [
    ("lead", "Lead", "amber"),
    ("active", "Active", "green"),
    ("dormant", "Dormant", "default"),
    ("churned", "Churned", "red"),
]


def register(registry: Registry) -> None:
    registry.add_resource(_companies())
    registry.add_resource(_contacts())


def _companies() -> Resource:
    return Resource(
        "companies",
        provider="db.main#companies",
        label="Company",
        label_plural="Companies",
        icon="▣",
        menu_group="Records",
        menu_order=20,
        display_field="name",
        default_sort=["name"],
        timeline=True,
        policy=DbPolicy(owner_field="owner", identity_attr="email"),
        fields=[
            TextField("id", label="ID", in_form=False, in_detail=False, in_list=False),
            TextField("name", required=True, searchable=True, inline_editable=True,
                      max_length=160, placeholder="Acme Corporation"),
            URLField("website", searchable=True, inline_editable=True),
            SelectField("industry", choices=INDUSTRIES, in_filter=True, inline_editable=True),
            SelectField("size", label="Headcount", choices=COMPANY_SIZES, in_filter=True),
            TextField("city", searchable=True, inline_editable=True),
            TextField("country", in_filter=True, inline_editable=True),
            TextField("owner", label="Account owner", in_filter=True, inline_editable=True),
            TextAreaField("notes", rows=4),
            # Contacts point back here; resolved by querying the contacts
            # resource rather than by a join, so the two could live in
            # different databases.
            BackrefField("contacts", resource="contacts", via="company_id",
                         label="Contacts", columns=("name", "email", "title", "status")),
            DateTimeField("created_at", label="Created", readonly=True, in_form=False),
        ],
        search=SearchSpec(
            fields=("name", "website", "city"),
            filters=("industry", "size", "country", "owner"),
            placeholder="Search",
        ),
        views=[
            ListView(
                columns=[
                    Column("name", link=True, width="26%"),
                    "industry", "size", "city", "country", "owner",
                    Column("created_at", label="Added"),
                ],
                default_sort=["name"],
                bulk_actions=["delete"],
            ),
            FormView([
                Section("Identity", ["name", "website", "industry", "size"], columns=2),
                Section("Location", ["city", "country"], columns=2),
                Section("Ownership", ["owner", "notes"], columns=1),
            ]),
            DetailView(
                sections=[
                    Section("Overview", ["name", "website", "industry", "size"], columns=2),
                    Section("Location", ["city", "country", "owner"], columns=3),
                    Section("Notes", ["notes"], columns=1),
                ],
                title_field="name",
                timeline=True,
            ),
        ],
    )


def _contacts() -> Resource:
    return Resource(
        "contacts",
        provider="db.main#contacts",
        label="Contact",
        icon="◑",
        menu_group="Records",
        menu_order=10,
        display_field="name",
        default_sort=["name"],
        timeline=True,
        policy=DbPolicy(owner_field="owner", identity_attr="email"),
        fields=[
            TextField("id", label="ID", in_form=False, in_detail=False, in_list=False),
            TextField("name", required=True, searchable=True, inline_editable=True,
                      max_length=120, placeholder="Ada Lovelace"),
            EmailField("email", required=True, searchable=True, inline_editable=True),
            PhoneField("phone", searchable=True, inline_editable=True),
            TextField("title", label="Job title", searchable=True, inline_editable=True),
            RelationField("company_id", label="Company", resource="companies",
                          display="name", search=("name",), in_filter=True),
            StatusField("status", choices=CONTACT_STATUSES, default="lead",
                        in_filter=True, inline_editable=True),
            MultiSelectField("tags", choices=["vip", "newsletter", "champion", "technical"]),
            TextField("owner", label="Owner", in_filter=True, inline_editable=True),
            BooleanField("subscribed", label="Email opt-in", default=True, inline_editable=True),
            TextAreaField("notes", rows=4),
            DateTimeField("last_contacted", label="Last contacted", inline_editable=True),
            ImageField("avatar", label="Photo", in_list=False),
            FileField("attachment", label="Attachment", in_list=False,
                      help="A contract, a business card, anything up to 25MB."),
            DateTimeField("created_at", label="Created", readonly=True, in_form=False),
        ],
        search=SearchSpec(
            fields=("name", "email", "phone", "title"),
            filters=("status", "company_id", "owner"),
        ),
        views=[
            ListView(
                columns=[
                    Column("name", link=True, width="20%"),
                    "email", "title", "company_id", "status", "owner",
                    Column("last_contacted", label="Last contact"),
                ],
                default_sort=["name"],
                bulk_actions=["delete"],
            ),
            FormView([
                Section("Person", ["name", "email", "phone", "title"], columns=2),
                Section("Relationship", ["company_id", "status", "owner", "subscribed"], columns=2),
                Section("Detail", ["tags", "last_contacted", "notes"], columns=1),
                Section("Files", ["avatar", "attachment"], columns=2),
            ]),
            DetailView(
                sections=[
                    Section("Contact", ["name", "email", "phone", "title"], columns=2),
                    Section("Relationship", ["company_id", "status", "owner", "subscribed"], columns=2),
                    Section("Detail", ["tags", "last_contacted", "notes"], columns=1),
                    Section("Files", ["avatar", "attachment"], columns=2),
                ],
                title_field="name",
                subtitle_field="title",
                timeline=True,
            ),
        ],
    )
