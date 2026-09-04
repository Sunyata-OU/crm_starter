"""Reading and writing the HTMX request/response protocol.

Every route serves three audiences from one handler: a browser asking for a
whole page, HTMX asking for a fragment to swap in, and an API client asking for
JSON. Recognising which is a header inspection, kept here so the routes do not
repeat it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from starlette.requests import Request
from starlette.responses import Response


@dataclass(frozen=True, slots=True)
class HtmxInfo:
    """What an HTMX request is telling us about itself."""

    is_htmx: bool = False
    #: True when HTMX is replacing the whole page (a boosted link).
    is_boosted: bool = False
    target: str = ""
    trigger: str = ""
    trigger_name: str = ""
    current_url: str = ""
    #: Set when HTMX is re-requesting after a history navigation.
    history_restore: bool = False

    @property
    def wants_fragment(self) -> bool:
        """Whether to render just the changed part of the page.

        A boosted request replaces the whole body, so it still needs the full
        page -- treating it as a fragment loses the navigation.
        """
        return self.is_htmx and not self.is_boosted and not self.history_restore


def read_htmx(request: Request) -> HtmxInfo:
    headers = request.headers
    return HtmxInfo(
        is_htmx=headers.get("HX-Request") == "true",
        is_boosted=headers.get("HX-Boosted") == "true",
        target=headers.get("HX-Target", ""),
        trigger=headers.get("HX-Trigger", ""),
        trigger_name=headers.get("HX-Trigger-Name", ""),
        current_url=headers.get("HX-Current-URL", ""),
        history_restore=headers.get("HX-History-Restore-Request") == "true",
    )


def wants_json(request: Request) -> bool:
    """Whether the caller asked for JSON rather than HTML.

    The path check comes first: an API route serves JSON regardless of what a
    browser's Accept header happens to say.
    """
    if request.url.path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "")
    if "application/json" not in accept:
        return False
    # Browsers send */* and text/html; only prefer JSON when it outranks HTML.
    return "text/html" not in accept


Level = Literal["success", "info", "warning", "error"]


def toast(response: Response, message: str, level: Level = "success") -> Response:
    """Attach a toast notification, fired client-side by HX-Trigger."""
    if message:
        _add_trigger(response, "crm:toast", {"message": message, "level": level})
    return response


def refresh(response: Response, *targets: str) -> Response:
    """Ask other parts of the page to re-request themselves.

    Used after a write so a list, a counter and a chart can each update without
    the handler knowing where any of them are on the page.
    """
    for target in targets:
        _add_trigger(response, f"crm:refresh:{target}", {})
    return response


def redirect(response: Response, url: str) -> Response:
    """Navigate the browser, in a way HTMX honours."""
    response.headers["HX-Redirect"] = url
    return response


def push_url(response: Response, url: str) -> Response:
    """Update the address bar without a navigation."""
    response.headers["HX-Push-Url"] = url
    return response


def reswap(response: Response, strategy: str) -> Response:
    """Override how HTMX swaps this response in."""
    response.headers["HX-Reswap"] = strategy
    return response


def retarget(response: Response, selector: str) -> Response:
    """Send this response somewhere other than the requesting element.

    Needed when a form submission fails: the errors belong in the form, not in
    the row the button lives in.
    """
    response.headers["HX-Retarget"] = selector
    return response


def _add_trigger(response: Response, event: str, detail: dict[str, Any]) -> None:
    """Merge into HX-Trigger, which may already carry other events."""
    existing = response.headers.get("HX-Trigger")
    payload: dict[str, Any] = {}
    if existing:
        try:
            payload = json.loads(existing)
        except json.JSONDecodeError:
            payload = {existing: {}}
    payload[event] = detail
    response.headers["HX-Trigger"] = json.dumps(payload)
