# PDF documents

Invoices, statements and reports are ordinary Jinja templates written in HTML
and CSS, rendered to HTML and then painted as a PDF by a *renderer*. There is
nothing new to learn beyond `@page` rules for paper.

## Install

```bash
pip install 'crm-starter[pdf]'      # WeasyPrint
# Debian/Ubuntu: apt install libpango-1.0-0 libpangoft2-1.0-0
```

It is optional on purpose. Without it a PDF request fails with "PDF generation
is not available", and nothing else is affected. `CRM_PDF_RENDERER` picks the
renderer (default `weasyprint`); another engine is one class with a
`render(html, *, base_url) -> bytes` method, registered with
`app.pdf.register_renderer(name, factory)`.

## A document on a resource

```python
from app.pdf.documents import PdfDocument

Resource("invoices", ..., documents=[
    PdfDocument(
        "invoice", template="pdf/invoice.html", label="Invoice PDF",
        filename="invoice-{number}.pdf",
        context=load_invoice_extras,   # (record, ctx) -> dict; sync or async
        roles=["finance"],             # optional; default: anyone who can read the record
    ),
])
```

This adds an **Invoice PDF** button on the record and the route
`GET /r/invoices/<pk>/pdf/invoice`. The route needs the same *read* permission
as the record and honours the row scope, so a PDF shows nothing the screen
would not. `?save=1` also files a copy in the default file store (under
`<resource>/<pk>/`); `inline=True` opens it in the browser instead of
downloading.

The template receives `record`, `resource`, `document`, `now`, `timezone`, plus
what `context` returns. The usual filters work (`money`, `date`, `datetime`).

## Starter templates

Copy these into your module's `templates/pdf/` and edit:

| Template | For | Context |
| --- | --- | --- |
| `pdf/invoice.html` | invoices | `record`, `seller`, `customer`, `lines`, `tax_rate`, `notes` |
| `pdf/report.html` | tabular reports | `title`, `subtitle`, `columns`, `rows`, `totals` |

Both extend `pdf/base.html` (A4, page numbers, print CSS).

## Reports with no record

```bash
crm pdf pdf/report.html -d data.json -o sales.pdf
```

or in code, `await app.pdf.documents.render_template(templates, renderer, "pdf/report.html", ctx)`.

## Storing and emailing

```python
from app.notify import Attachment, Notification
from app.pdf.store import save_pdf

stored = await save_pdf(content, "invoice-42.pdf", folder="invoices/42")
await notifier.send(Notification(
    recipient=email, title="Your invoice",
    attachments=(Attachment("invoice-42.pdf", content),),
), ctx)
```

Attachments are held in memory and only the email channel delivers them. They
are not stored with the notification row, so a *scheduled* notification
cannot carry one (it raises); generate and send in the same step.

## Security

Templates are not trusted with the network or the disk. The built-in renderer
loads only `data:` URIs and files under the static directory; any other URL
(`http://…`, `file:///etc/passwd`) is refused and the page renders without it.
Keep logos in `static/` or inline them as `data:` URIs. Output is autoescaped
like every other template, so a customer name containing markup is printed, not
interpreted.
