"""The notification bell.

Three routes: what is waiting, dismissing one, and dismissing all. The bell
polls rather than holding a connection open -- a CRM notification is not
urgent to the second, and polling costs nothing next to the complexity of
keeping a socket alive through a load balancer.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.responses import Response

from app.notify import notifier
from app.web import htmx
from app.web.deps import View, build_view

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", name="notifications")
async def bell(view: View = Depends(build_view)) -> Response:
    """The dropdown's contents."""
    view.require_login()
    entries = await notifier.unread_for(view.identity, view.ctx, limit=15)
    if view.wants_json:
        return view.json({
            "count": len(entries),
            "items": [dict(e) for e in entries],
        })
    return view.render("notifications/_panel.html", entries=entries)


def _inapp_enabled(view: View) -> bool:
    """Whether this deployment runs the in-app channel at all.

    The bell is a table, and a deployment can be configured without one. The
    header stops rendering the bell in that case, but a page loaded before the
    setting changed keeps polling on its own timer -- so the answer is zero
    here rather than an error from a store that was never meant to be read.
    """
    return "inapp" in view.settings.notify_channels


@router.get("/count")
async def unread_count(view: View = Depends(build_view)) -> Response:
    """Just the number, for the badge.

    Its own route so the header can refresh cheaply without rendering the list.
    """
    if not view.identity.is_authenticated or not _inapp_enabled(view):
        return view.render("notifications/_badge.html", count=0)
    count = await notifier.count_unread(view.identity, view.ctx)
    if view.wants_json:
        return view.json({"count": count})
    return view.render("notifications/_badge.html", count=count)


@router.post("/{pk}/read")
async def dismiss(pk: str, view: View = Depends(build_view)) -> Response:
    """Dismiss one notification."""
    view.require_login()
    # The service checks the notification belongs to this caller; a
    # notification is addressed to one person and nobody else may dismiss it.
    await notifier.mark_read(pk, view.identity, view.ctx)
    entries = await notifier.unread_for(view.identity, view.ctx, limit=15)
    response = view.render("notifications/_panel.html", entries=entries)
    return htmx.refresh(response, "notifications")


@router.post("/read-all")
async def dismiss_all(view: View = Depends(build_view)) -> Response:
    """Dismiss everything waiting."""
    view.require_login()
    cleared = await notifier.mark_all_read(view.identity, view.ctx)
    response = view.render("notifications/_panel.html", entries=[])
    htmx.refresh(response, "notifications")
    return view.toast(response, f"Cleared {cleared} notification(s).", "success")
