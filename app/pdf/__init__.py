"""Turning a template into a PDF.

A document is an ordinary Jinja template written in HTML and CSS -- the same
skills and the same template stack as every screen -- rendered to HTML and then
handed to a *renderer*, which paints the pages. The renderer is a one-method
interface so a deployment that prefers another engine registers it by name and
sets ``CRM_PDF_RENDERER``; the built-in one is WeasyPrint.

WeasyPrint is an **optional dependency** (``pip install 'crm-starter[pdf]'``): it
needs the Pango libraries on the host, which a deployment that never prints
anything should not be made to install. Asking for a PDF without it fails with
a plain "not installed" error, not an ImportError traceback.

**Templates are not trusted with the network or the disk.** A renderer follows
every ``<img src>``, ``<link>`` and ``url()`` it finds, and the data in a
document -- a customer's company name, a free-text note -- can contain markup
that autoescaping missed or that arrives pre-rendered. Left alone that is a
server-side request forgery and a local-file read in one. The built-in renderer
therefore fetches only ``data:`` URIs and files under the application's static
directory; anything else is refused and the page renders without it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from app.core.errors import CRMError

log = logging.getLogger("crm.pdf")

PDF_CONTENT_TYPE = "application/pdf"


class PdfError(CRMError):
    status_code = 500
    public_message = "The PDF could not be produced."


class PdfUnavailable(PdfError):
    status_code = 501
    public_message = "PDF generation is not available on this server."


class PdfRenderer(Protocol):
    """Paints HTML as a PDF. Synchronous; callers run it off the event loop."""

    name: str

    def render(self, html: str, *, base_url: str | None = None) -> bytes: ...


def safe_fetcher(static_dir: Path | None) -> Any:
    """A WeasyPrint URL fetcher allowing ``data:`` URIs and files under ``static_dir`` only.

    Called as ``fetcher.fetch(url)``; anything else raises ``ValueError``,
    which WeasyPrint logs and renders around (the page loses the image, it does
    not fail).
    """
    from urllib.parse import unquote, urlparse

    from weasyprint.urls import URLFetcher

    root = static_dir.resolve() if static_dir else None

    class SafeFetcher(URLFetcher):
        def fetch(self, url: str, headers: Any = None) -> Any:
            if url.startswith("data:"):
                return super().fetch(url, headers)
            if root is not None and url.startswith("file://"):
                path = Path(unquote(urlparse(url).path)).resolve()
                if path.is_relative_to(root):
                    return super().fetch(url, headers)
            raise ValueError(f"blocked fetch of {url[:80]!r} while rendering a PDF")

    return SafeFetcher(allowed_protocols={"data", "file"})


class WeasyPrintRenderer:
    name = "weasyprint"

    def __init__(self, *, static_dir: Path | None = None) -> None:
        try:
            import weasyprint  # noqa: F401
        except (ImportError, OSError) as exc:  # OSError: Pango shared libraries missing
            raise PdfUnavailable(
                "WeasyPrint is not installed (pip install 'crm-starter[pdf]', "
                f"plus the Pango libraries): {exc}"
            ) from exc
        self.static_dir = static_dir

    def render(self, html: str, *, base_url: str | None = None) -> bytes:
        import weasyprint

        base = base_url or (self.static_dir.as_uri() + "/" if self.static_dir else None)
        document = weasyprint.HTML(
            string=html, base_url=base, url_fetcher=safe_fetcher(self.static_dir)
        )
        return bytes(document.write_pdf())


_FACTORIES: dict[str, Callable[..., PdfRenderer]] = {"weasyprint": WeasyPrintRenderer}


def register_renderer(name: str, factory: Callable[..., PdfRenderer]) -> None:
    """Make a renderer selectable with ``CRM_PDF_RENDERER=<name>``."""
    _FACTORIES[name] = factory


def build_renderer(settings: Any) -> PdfRenderer:
    name = getattr(settings, "pdf_renderer", "weasyprint")
    try:
        factory = _FACTORIES[name]
    except KeyError:
        known = ", ".join(sorted(_FACTORIES))
        raise PdfUnavailable(f"unknown PDF renderer {name!r} (known: {known})") from None
    return factory(static_dir=getattr(settings, "static_dir", None))


async def render_html(renderer: PdfRenderer, html: str, *, base_url: str | None = None) -> bytes:
    """Render off the event loop: painting a long document is CPU work."""
    try:
        return await asyncio.to_thread(renderer.render, html, base_url=base_url)
    except PdfError:
        raise
    except Exception as exc:
        log.exception("PDF rendering failed")
        raise PdfError(f"{type(exc).__name__}: {exc}") from exc
