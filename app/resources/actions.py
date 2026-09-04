"""Actions -- the buttons that do something other than edit a field.

An action is declared once and can appear on a row, in a detail header, or as a
bulk operation over a selection. What it does is a plain async callable, so
sending a message, calling an API or running local logic all look the same from
the template's point of view.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from app.core.results import Ctx, Identity, Record, WriteResult

if TYPE_CHECKING:
    from app.resources.resource import Resource

#: Where an action may be offered.
Placement = Literal["row", "detail", "list", "bulk"]


@dataclass(slots=True)
class ActionResult:
    """What happened, and what the browser should do about it."""

    message: str = ""
    level: Literal["success", "info", "warning", "error"] = "success"
    #: Navigate here afterwards.
    redirect: str = ""
    #: Re-read the affected records before re-rendering.
    refresh: bool = True
    #: Extra values for the template when the action renders its own fragment.
    data: dict[str, Any] = dc_field(default_factory=dict)

    @classmethod
    def from_write(cls, result: WriteResult, *, done: str = "Done.") -> ActionResult:
        """Translate a provider write into user-facing feedback.

        A queued write is reported as such rather than as success, so the user
        is not told a change landed when it has only been accepted.
        """
        if result.failed:
            return cls(message=result.message or "That did not work.", level="error", refresh=False)
        if result.status.value == "pending":
            return cls(message=result.message or "Queued.", level="info")
        return cls(message=result.message or done, level="success")


class ActionHandler(Protocol):
    """Called with the records the action applies to."""

    async def __call__(
        self, records: Sequence[Record], ctx: Ctx, resource: Resource
    ) -> ActionResult | None: ...


class Action:
    """A named operation offered on a resource."""

    def __init__(
        self,
        name: str,
        label: str = "",
        *,
        handler: ActionHandler | None = None,
        icon: str = "",
        placements: Sequence[Placement] = ("row", "detail"),
        #: Ask before running. Destructive actions should always set this.
        confirm: str = "",
        roles: Sequence[str] = (),
        #: Only offered when this returns True for the record.
        available: Callable[[Record, Identity], bool] | None = None,
        style: Literal["default", "primary", "danger"] = "default",
        #: Navigate instead of calling a handler. Formatted with the record.
        url: str = "",
        #: Collect these fields in a dialog and pass them to the handler.
        prompt_fields: Sequence[str] = (),
    ) -> None:
        if handler is None and not url:
            raise ValueError(f"action {name!r} needs either a handler or a url")
        self.name = name
        self.label = label or name.replace("_", " ").capitalize()
        self.handler = handler
        self.icon = icon
        self.placements = tuple(placements)
        self.confirm = confirm
        self.roles = frozenset(roles)
        self.available = available
        self.style = style
        self.url = url
        self.prompt_fields = tuple(prompt_fields)

    def allowed_for(self, identity: Identity) -> bool:
        """Role check, independent of any particular record."""
        return not self.roles or identity.has_role(*self.roles)

    def visible_for(self, record: Record, identity: Identity) -> bool:
        """Whether to draw this action next to ``record``."""
        if not self.allowed_for(identity):
            return False
        return self.available is None or self.available(record, identity)

    def shown_in(self, placement: Placement) -> bool:
        return placement in self.placements

    def resolve_url(self, record: Record | None) -> str:
        """Fill ``{field}`` placeholders in a url action from the record."""
        if not self.url or record is None:
            return self.url
        try:
            return self.url.format(**record)
        except (KeyError, IndexError):
            # A placeholder referencing a column this record did not return
            # should disable the link, not raise mid-render.
            return ""

    def __repr__(self) -> str:
        return f"<Action {self.name!r}>"


def action(
    name: str, label: str = "", **options: Any
) -> Callable[[ActionHandler], Action]:
    """Decorator form: turn an async function into an :class:`Action`.

        @action("send_welcome", "Send welcome email", style="primary")
        async def send_welcome(records, ctx, resource):
            ...
    """

    def decorator(fn: ActionHandler) -> Action:
        return Action(name, label, handler=fn, **options)

    return decorator
