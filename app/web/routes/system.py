"""Health, diagnostics and the dashboard."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from starlette.responses import Response

from app.auth.permissions import require_roles
from app.core.query import Agg, AggSpec, Measure
from app.web.deps import View, build_view

router = APIRouter(tags=["system"])


@router.get("/", name="dashboard")
async def dashboard(view: View = Depends(build_view)) -> Response:
    """A landing page with a count per resource the caller can see."""
    view.require_login()

    visible = [
        r for r in view.registry.resources
        if r.in_menu and r.policy.allows("read", view.identity)
    ]

    async def count_of(resource) -> dict:
        try:
            rows = await resource.provider.aggregate(
                AggSpec(
                    measures=(Measure(Agg.COUNT, alias="count"),),
                    scope=resource.policy.scope(view.identity),
                ),
                view.ctx,
            )
            return {"resource": resource, "count": rows[0]["count"] if rows else 0, "error": ""}
        except Exception as exc:
            # One unreachable backend must not blank the whole dashboard; show
            # the card with its problem instead.
            return {"resource": resource, "count": None, "error": str(exc)}

    # Counted concurrently. These are independent round trips, possibly to
    # different systems, so doing them in sequence makes the dashboard as slow
    # as the sum of every backend rather than as slow as the slowest one.
    cards = list(await asyncio.gather(*(count_of(r) for r in visible)))

    return view.render(
        "dashboard.html",
        cards=cards,
        # So an empty dashboard can say which of the two things went wrong:
        # nothing registered, or nothing this caller may see.
        registered=any(r.in_menu for r in view.registry.resources),
        title="Dashboard",
    )


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness. Deliberately does not touch any backend."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(view: View = Depends(build_view)) -> Response:
    """Readiness: reports every configured connection."""
    checks = await view.registry.connections.health_all()
    healthy = all(c.healthy for c in checks)
    return view.json(
        {
            "status": "ok" if healthy else "degraded",
            "connections": [
                {"name": c.name, "type": c.type, "healthy": c.healthy, "detail": c.detail}
                for c in checks
            ],
        },
        status_code=200 if healthy else 503,
    )


@router.get("/system")
async def system_page(view: View = Depends(build_view)) -> Response:
    """What is wired up: connections, resources and their real capabilities.

    Worth having in the UI rather than only the CLI: when a screen is missing a
    button, the answer is usually that the backend behind it cannot do that
    operation, and this page says so directly.
    """
    require_roles(view.identity, "admin")

    connections = await view.registry.connections.health_all()
    auth_health = await view.state.auth.health()

    resources = []
    for resource in view.registry.resources:
        provider = resource.provider
        caps = getattr(provider, "capabilities", None)
        inner = getattr(provider, "inner_capabilities", caps)
        resources.append(
            {
                "resource": resource,
                "provider": getattr(provider, "name", "?"),
                "shimmed": inner is not caps,
                "capabilities": caps,
                "native": inner,
            }
        )

    return view.render(
        "system.html",
        connections=connections,
        auth_health=auth_health,
        resources=resources,
        providers=[p.name for p in view.state.auth],
        title="System",
    )
