"""Where other systems tell the back office that work exists.

One route, for machine callers. It is authenticated like the rest of the JSON
API -- a bearer token -- and additionally needs the ``integration`` role, so a
token issued for something else cannot raise tasks by accident. Bearer calls
are exempt from CSRF (no ambient cookie to forge), which is what lets a service
post here at all.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.responses import Response

from app import task_events
from app.core.errors import PermissionDenied, ValidationFailed
from app.web.deps import View, build_view

router = APIRouter(prefix="/events", tags=["events"])

#: The role a token needs to post events.
ROLE = "integration"


@router.post("/tasks", name="task_event")
async def task_event(view: View = Depends(build_view)) -> Response:
    """Raise or resolve a task. See :mod:`app.task_events` for the contract."""
    view.require_login()
    if not view.identity.has_role(ROLE):
        raise PermissionDenied()
    try:
        payload = await view.request.json()
    except ValueError:
        raise ValidationFailed({"body": "must be a JSON object"}) from None
    if not isinstance(payload, dict):
        raise ValidationFailed({"body": "must be a JSON object"})
    outcome = await task_events.apply(view.registry, payload, view.ctx)
    return view.json(outcome, status_code=201 if outcome["status"] == "created" else 200)
