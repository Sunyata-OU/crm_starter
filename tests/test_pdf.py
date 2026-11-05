"""PDF generation: documents on resources, the renderer seam, safe fetching, attachments."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.registry import Registry
from app.fields.types import IntegerField, TextField
from app.main import create_app
from app.notify.base import Attachment, Notification
from app.notify.channels import EmailChannel
from app.pdf import PdfError, PdfUnavailable, build_renderer, register_renderer
from app.pdf.documents import PdfDocument
from app.providers.memory import MemoryProvider
from app.resources.resource import Resource
from app.resources.views import Column, ListView
from app.settings import Settings
from tests.conftest import sign_in

PDF_MAGIC = b"%PDF-"


class FakeRenderer:
    """Records the HTML it was given and returns something PDF-shaped."""

    name = "fake"
    seen: list[str] = []

    def __init__(self, **_: object) -> None:
        pass

    def render(self, html: str, *, base_url: str | None = None) -> bytes:
        FakeRenderer.seen.append(html)
        return PDF_MAGIC + b"fake"


register_renderer("fake", FakeRenderer)

INVOICES = [{"id": 1, "number": "INV/7 2026", "customer": "Acme <Ltd>", "secret": "x"}]


async def lines(record, ctx):
    return {"lines": [{"description": "Work", "quantity": 2, "unit_price": 10}],
            "seller": {"name": "Us"}, "customer": {"name": record["customer"]}}


def build_registry(**doc_kw) -> Registry:
    registry = Registry()
    registry.add_resource(
        Resource(
            "invoices",
            provider=MemoryProvider(INVOICES),
            display_field="number",
            fields=[IntegerField("id", in_list=False), TextField("number"),
                    TextField("customer"), TextField("secret", read_roles=["admin"])],
            views=[ListView(columns=[Column("number")])],
            documents=[PdfDocument("invoice", template="pdf/invoice.html",
                                   filename="invoice-{number}.pdf", context=lines, **doc_kw)],
        )
    )
    return registry


def make_client(**doc_kw) -> TestClient:
    FakeRenderer.seen.clear()
    settings = Settings(
        environment="test", secret_key="test-key-not-for-real-use", debug=False,
        template_reload=False, csrf_enabled=True, auth_providers=["session", "local"],
        modules=[], pdf_renderer="fake",
    )
    app = create_app(settings=settings, registry=build_registry(**doc_kw))
    return TestClient(app, raise_server_exceptions=False)


class TestTheRecordRoute:
    def test_it_returns_a_pdf_named_from_the_record(self):
        with make_client() as client:
            sign_in(client, email="a@example.com", roles=["admin"])
            response = client.get("/r/invoices/1/pdf/invoice")
        assert response.status_code == 200
        assert response.content.startswith(PDF_MAGIC)
        assert response.headers["content-type"] == "application/pdf"
        # A slash and a space in the number cannot reach the header.
        assert 'attachment; filename="invoice-INV_7_2026.pdf"' in response.headers["content-disposition"]

    def test_the_template_sees_the_record_and_escapes_it(self):
        with make_client() as client:
            sign_in(client, email="a@example.com", roles=["admin"])
            client.get("/r/invoices/1/pdf/invoice")
        html = FakeRenderer.seen[0]
        assert "Acme &lt;Ltd&gt;" in html and "Acme <Ltd>" not in html
        assert "INV/7 2026" in html

    def test_it_needs_a_signed_in_reader(self):
        with make_client() as client:
            response = client.get("/r/invoices/1/pdf/invoice", follow_redirects=False)
        assert response.status_code in (302, 303, 401, 403)
        assert not FakeRenderer.seen

    def test_roles_can_narrow_who_may_produce_it(self):
        with make_client(roles=["finance"]) as client:
            sign_in(client, email="a@example.com", roles=["admin"])
            response = client.get("/r/invoices/1/pdf/invoice")
        assert response.status_code == 403
        assert not FakeRenderer.seen

    def test_an_unknown_document_or_record_is_a_404(self):
        with make_client() as client:
            sign_in(client, email="a@example.com", roles=["admin"])
            assert client.get("/r/invoices/1/pdf/nope").status_code == 404
            assert client.get("/r/invoices/99/pdf/invoice").status_code == 404

    def test_the_record_page_offers_the_button(self):
        with make_client() as client:
            sign_in(client, email="a@example.com", roles=["admin"])
            page = client.get("/r/invoices/1")
        assert "/r/invoices/1/pdf/invoice" in page.text

    def test_save_files_a_copy(self):
        from app.storage import MemoryFileStore, store

        memory = MemoryFileStore()
        store.bind("files", memory)
        with make_client() as client:
            sign_in(client, email="a@example.com", roles=["admin"])
            response = client.get("/r/invoices/1/pdf/invoice?save=1")
        assert response.status_code == 200
        assert any(key.endswith(".pdf") for key in memory.files)  # noqa: SLF001


class TestTheRendererSeam:
    def test_an_unknown_renderer_is_named_in_the_error(self):
        with pytest.raises(PdfUnavailable, match="nope"):
            build_renderer(Settings(pdf_renderer="nope"))

    def test_a_failing_renderer_surfaces_as_a_pdf_error(self):
        import asyncio

        from app.pdf import render_html

        class Boom:
            name = "boom"

            def render(self, html, *, base_url=None):
                raise RuntimeError("kaput")

        with pytest.raises(PdfError, match="kaput"):
            asyncio.run(render_html(Boom(), "<p>x</p>"))


class TestTemplates:
    def test_the_starter_report_renders_rows(self):
        from app.web.render import Templates

        html = Templates(Settings(modules=[])).render(
            "pdf/report.html",
            {"title": "Sales", "columns": ["Who", "Total"], "rows": [["Ann", 5]],
             "totals": ["", 5], "now": __import__("datetime").datetime(2026, 1, 1)},
        )
        assert "<h1>Sales</h1>" in html and "Ann" in html


class TestAttachments:
    def test_the_email_carries_the_file(self):
        channel = EmailChannel(sender="crm@example.com")
        message = channel.build_message(Notification(
            recipient="kim@example.com", title="Your invoice",
            attachments=(Attachment("inv.pdf", PDF_MAGIC + b"x"),),
        ))
        (part,) = list(message.iter_attachments())
        assert part.get_filename() == "inv.pdf"
        assert part.get_content_type() == "application/pdf"
        assert part.get_payload(decode=True) == PDF_MAGIC + b"x"

    def test_a_scheduled_notification_cannot_hold_one(self):
        from datetime import timedelta

        from app.core.clock import utcnow

        with pytest.raises(ValueError, match="scheduled"):
            Notification(recipient="a@b.c", title="t", due_at=utcnow() + timedelta(days=1),
                         attachments=(Attachment("a.pdf", b"x"),))


try:
    import weasyprint  # noqa: F401

    HAS_WEASYPRINT = True
except (ImportError, OSError):  # not installed, or Pango missing on this host
    HAS_WEASYPRINT = False


@pytest.mark.skipif(not HAS_WEASYPRINT, reason="WeasyPrint is optional")
class TestWeasyPrint:
    def render(self, html: str, tmp_path) -> bytes:
        from app.pdf import WeasyPrintRenderer

        return WeasyPrintRenderer(static_dir=tmp_path).render(html)

    def test_it_produces_a_pdf(self, tmp_path):
        assert self.render("<h1>Hello</h1>", tmp_path).startswith(PDF_MAGIC)

    def test_it_will_not_read_files_outside_the_static_directory(self, tmp_path):
        from app.pdf import safe_fetcher

        secret = tmp_path.parent / "secret.txt"
        secret.write_text("hunter2")
        fetcher = safe_fetcher(tmp_path)
        with pytest.raises(ValueError, match="blocked"):
            fetcher.fetch(secret.as_uri())
        with pytest.raises(ValueError, match="blocked"):
            fetcher.fetch("http://169.254.169.254/latest/meta-data/")

    def test_it_reads_data_uris_and_static_files(self, tmp_path):
        from app.pdf import safe_fetcher

        (tmp_path / "logo.txt").write_text("ok")
        fetcher = safe_fetcher(tmp_path)
        assert fetcher.fetch("data:text/plain;base64,aGk=").read() == b"hi"
        assert fetcher.fetch((tmp_path / "logo.txt").as_uri()).read() == b"ok"

    def test_a_blocked_image_does_not_fail_the_page(self, tmp_path):
        out = self.render('<img src="http://127.0.0.1:9/x.png"><p>body</p>', tmp_path)
        assert out.startswith(PDF_MAGIC)
