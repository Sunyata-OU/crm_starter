"""Documents: a template bound to a resource, offered as a PDF.

Declared on a resource::

    Resource("invoices", ..., documents=[
        PdfDocument("invoice", template="pdf/invoice.html", label="Invoice PDF",
                    filename="invoice-{number}.pdf",
                    context=lambda record, ctx: {"lines": ...}),
    ])

That adds a "Invoice PDF" button on the record and a route,
``/r/invoices/<pk>/pdf/invoice``. The route is gated by the same *read*
permission as the record itself: a PDF shows nothing the screen would not.

The template gets ``record``, ``resource``, ``document``, ``now`` and
``timezone``, plus whatever ``context`` returns (sync or async). Standalone
reports with no record use :func:`render_template`.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from app.core.clock import utcnow
from app.core.results import Ctx, Record
from app.pdf import PdfRenderer, render_html

if TYPE_CHECKING:
    from app.resources.actions import Action
    from app.resources.resource import Resource
    from app.web.render import Templates

ContextBuilder = Callable[[Record, Ctx], Mapping[str, Any]]


class PdfDocument:
    def __init__(
        self,
        name: str,
        *,
        template: str,
        label: str = "",
        #: ``str.format``-ed with the record; unsafe characters are removed.
        filename: str = "{resource}-{pk}.pdf",
        #: Extra values for the template, from the record. May be ``async``.
        context: ContextBuilder | None = None,
        #: Restrict the button (and the route) to these roles. Empty: anyone who can read.
        roles: Sequence[str] = (),
        #: Open in the browser's viewer rather than downloading.
        inline: bool = False,
    ) -> None:
        self.name = name
        self.template = template
        self.label = label or name.replace("_", " ").capitalize()
        self.filename = filename
        self.context = context
        self.roles = frozenset(roles)
        self.inline = inline

    def allowed_for(self, identity: Any) -> bool:
        return not self.roles or bool(self.roles & set(identity.roles))

    def file_name(self, resource: Resource, record: Record) -> str:
        raw = self.filename.format(resource=resource.name, pk=record.pk, **record)
        return re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._") or "document.pdf"

    def action(self, resource: Resource) -> Action:
        """The record's button: a plain link to the route."""
        from app.resources.actions import Action

        pk_field = resource.pk if isinstance(resource.pk, str) else "id"
        return Action(
            f"pdf_{self.name}",
            self.label,
            url=f"/r/{resource.name}/{{{pk_field}}}/pdf/{self.name}",
            placements=("detail",),
            roles=tuple(self.roles),
        )


async def render_template(
    templates: Templates,
    renderer: PdfRenderer,
    template: str,
    context: Mapping[str, Any],
    *,
    timezone: str = "UTC",
) -> bytes:
    """Render any template to PDF -- the entry point for reports with no record."""
    html = templates.render(
        template, {"now": utcnow(), "timezone": timezone, **context}
    )
    return await render_html(renderer, html)


async def render_document(
    document: PdfDocument,
    resource: Resource,
    record: Record,
    ctx: Ctx,
    templates: Templates,
    renderer: PdfRenderer,
) -> bytes:
    extra: Mapping[str, Any] = {}
    if document.context is not None:
        built = document.context(record, ctx)
        extra = await built if inspect.isawaitable(built) else built
    return await render_template(
        templates, renderer, document.template,
        {"record": record, "resource": resource, "document": document, **extra},
        timezone=ctx.timezone,
    )
