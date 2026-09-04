"""A resource that does not live in the database.

This module exists to prove the central claim: the same declaration, the same
screens, and a completely different kind of backend behind them.

It is optional and off by default, because it needs services the demo does not
start. Enable it with ``CRM_MODULES=demo_crm,demo_sales,demo_remote`` once the
``api.reference`` and ``mq.events`` connections in ``connections.yaml`` point
somewhere real.

Two things are worth reading here:

* ``support_tickets`` is read from an HTTP API that cannot filter, sort or
  count. It gets a working list view with filters, sorting and pagination
  anyway, because the capability shim supplies what the endpoint lacks.
* ``provisioning_requests`` reads from the database but *writes* by publishing
  a message. Its forms look identical to any other resource's; the difference
  is that saving reports "queued" rather than claiming the record was stored,
  because that is the truth.
"""

from __future__ import annotations

from app.core.registry import Registry
from app.fields.types import (
    DateTimeField,
    EmailField,
    SelectField,
    StatusField,
    TextAreaField,
    TextField,
)
from app.resources.policy import ReadOnlyPolicy
from app.resources.resource import Resource
from app.resources.views import Column, FormView, ListView, SearchSpec, Section

MANIFEST = {
    "name": "demo_remote",
    "label": "Remote backends",
    "description": "A read-only API resource and a queue-backed one.",
    "depends": ("demo_crm",),
    "menu_groups": {"Integrations": 50},
    # Needs external services, so it is never loaded implicitly.
    "optional": True,
}

TICKET_STATES = [
    ("new", "New", "amber"),
    ("open", "Open", "blue"),
    ("pending", "Pending", "default"),
    ("solved", "Solved", "green"),
]


def register(registry: Registry) -> None:
    registry.add_resource(_support_tickets())
    registry.add_resource(_provisioning_requests())


def _support_tickets() -> Resource:
    """Read-only, over an HTTP API.

    ``rest_mapping`` describes the endpoint's shape. Note what it does *not*
    declare: no ``filter_params``, no ``sort_param``, no ``total_key``. The
    provider therefore advertises none of those, and the shim handles them --
    visible on the /system page as "emulated".
    """
    resource = Resource(
        "support_tickets",
        provider="api.reference#/tickets",
        label="Ticket",
        icon="◍",
        menu_group="Integrations",
        display_field="subject",
        default_sort=["-created_at"],
        policy=ReadOnlyPolicy(),
        fields=[
            TextField("id", label="Reference", in_form=False),
            TextField("subject", searchable=True),
            StatusField("status", choices=TICKET_STATES, in_filter=True),
            SelectField("priority", choices=["low", "normal", "high", "urgent"], in_filter=True),
            EmailField("requester_email", label="Requester", searchable=True),
            TextAreaField("description"),
            DateTimeField("created_at", label="Raised"),
        ],
        search=SearchSpec(fields=("subject", "requester_email"), filters=("status", "priority")),
        views=[
            ListView(
                columns=[
                    Column("id", width="10%"),
                    Column("subject", link=True, width="40%"),
                    "status", "priority", "requester_email", "created_at",
                ],
                default_sort=["-created_at"],
                # No inline editing: the backend is read-only, and offering an
                # edit that cannot be saved is worse than not offering it.
                inline_edit=False,
            ),
        ],
    )
    # Read by the REST provider factory.
    resource.rest_mapping = {
        "path": "/tickets",
        "items_key": "results",
        "total_key": "",
        "pagination": "none",
    }
    return resource


def _provisioning_requests() -> Resource:
    """Reads from the database, writes by publishing a command.

    The provider reference names the read side; ``write_provider`` names the
    write side, and the registry combines them. Because the write half is
    asynchronous, every save returns PENDING and the UI says "queued".
    """
    resource = Resource(
        "provisioning_requests",
        provider="db.main#provisioning_requests",
        label="Provisioning request",
        icon="◎",
        menu_group="Integrations",
        display_field="account_name",
        default_sort=["-created_at"],
        fields=[
            TextField("id", in_form=False),
            TextField("account_name", label="Account", required=True, searchable=True),
            SelectField("plan", choices=["starter", "team", "enterprise"], required=True,
                        in_filter=True),
            StatusField(
                "state",
                choices=[("queued", "Queued", "amber"), ("active", "Active", "green"),
                         ("failed", "Failed", "red")],
                readonly=True,
                in_form=False,
                in_filter=True,
            ),
            TextAreaField("notes"),
            DateTimeField("created_at", label="Requested", readonly=True, in_form=False),
        ],
        views=[
            ListView(
                columns=[Column("account_name", link=True), "plan", "state", "created_at"],
                # The state is owned by the consumer of the queue, not by us.
                inline_edit=False,
            ),
            FormView(
                [Section("Request", ["account_name", "plan", "notes"], columns=2)],
                submit_label="Submit request",
            ),
        ],
    )
    # Combined with the read side by the registry at bind time.
    resource.write_provider = "mq.events#provisioning"
    return resource
