"""Actions -- the buttons that do something other than edit a field.

An action is declared once and can appear on a row, in a detail header, or as a
bulk operation over a selection. What it does is a plain async callable, so
sending a message, calling an API or running local logic all look the same from
the template's point of view.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from app.core.errors import ConfigError
from app.core.results import Ctx, Identity, Record, WriteResult
from app.fields.base import Field

if TYPE_CHECKING:
    from app.resources.resource import Resource

#: Where an action may be offered.
Placement = Literal["row", "detail", "list", "bulk"]


@dataclass(slots=True, frozen=True)
class RowOutcome:
    """What became of one record in a batch.

    A bulk action over forty rows is forty separate attempts against, usually,
    a remote service. Collapsing that into one "Done." is the lie people
    discover a week later, so a handler can say what happened to each row and
    the framework reports the shape of it.
    """

    pk: Any
    ok: bool
    message: str = ""


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
    #: Per-record results, when the action ran over several.
    outcomes: tuple[RowOutcome, ...] = ()

    @classmethod
    def from_outcomes(
        cls, outcomes: Sequence[RowOutcome], *, done: str = "Done", **kw: Any
    ) -> ActionResult:
        """Summarise a batch honestly: how many worked, and why the rest did not.

        Mixed results are a warning rather than a success. The failures are
        named in the message -- up to a few of them -- because "3 failed" sends
        somebody to the logs, and the reason is usually one sentence.
        """
        ok = [o for o in outcomes if o.ok]
        bad = [o for o in outcomes if not o.ok]
        if not bad:
            return cls(message=f"{done}: {len(ok)}.", level="success",
                       outcomes=tuple(outcomes), **kw)

        reasons = []
        for outcome in bad[:3]:
            reasons.append(f"{outcome.pk}: {outcome.message}" if outcome.message
                           else str(outcome.pk))
        detail = "; ".join(reasons)
        if len(bad) > 3:
            detail += f"; and {len(bad) - 3} more"

        if not ok:
            return cls(message=f"None of {len(bad)} worked -- {detail}.", level="error",
                       refresh=False, outcomes=tuple(outcomes), **kw)
        return cls(message=f"{done}: {len(ok)}. {len(bad)} failed -- {detail}.",
                   level="warning", outcomes=tuple(outcomes), **kw)

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
    """Called with the records the action applies to.

    A handler that declares a ``params`` argument is given the values collected
    by ``prompt_fields``; one that does not is called with three arguments as
    before. That is deliberate: a parameterless action is the common case and
    should not have to accept an argument it will never read.
    """

    async def __call__(
        self, records: Sequence[Record], ctx: Ctx, resource: Resource
    ) -> ActionResult | None: ...


class ParameterisedHandler(Protocol):
    """The other shape: a handler that asked for ``prompt_fields``.

    Two protocols rather than one with an optional argument, because the
    difference is not a default value -- it is which of the two calls `run`
    makes, decided by inspecting the handler. Naming both keeps that decision
    checkable instead of hidden behind an ``Any``.
    """

    async def __call__(
        self, records: Sequence[Record], ctx: Ctx, resource: Resource,
        *, params: dict[str, Any],
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
        #: Collect these before running, in a dialog, and pass them to the
        #: handler as ``params``.
        #:
        #: Either names of the resource's own fields, or `Field` instances for
        #: values the resource does not store -- a ban reason, a cancellation
        #: note, an "until" date. The second form is the common one: what an
        #: action needs to know is rarely a column on the thing it acts upon.
        prompt_fields: Sequence[str | Field] = (),
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

    def prompts(self, resource: Resource) -> list[Field]:
        """The fields to collect, resolved against ``resource``.

        Resolution happens here rather than at declaration time because an
        action may be declared before the resource it is attached to, and a
        name that does not resolve should say so with both names in the message
        rather than failing later as a missing form value.
        """
        fields: list[Field] = []
        for spec in self.prompt_fields:
            if isinstance(spec, Field):
                fields.append(spec)
                continue
            field = resource.get_field(spec)
            if field is None:
                raise ConfigError(
                    f"action {self.name!r} prompts for {spec!r}, which resource "
                    f"{resource.name!r} does not declare; pass a Field instance "
                    f"if the value is not a column on the resource"
                )
            fields.append(field)
        return fields

    @property
    def prompts_for_input(self) -> bool:
        """Whether running this needs a dialog first."""
        return bool(self.prompt_fields)

    async def run(
        self,
        records: Sequence[Record],
        ctx: Ctx,
        resource: Resource,
        params: dict[str, Any] | None = None,
    ) -> ActionResult | None:
        """Call the handler, passing collected values only if it wants them."""
        if self.handler is None:
            raise ValueError(f"action {self.name!r} has no handler to run")
        if self._handler_takes_params():
            takes_params = cast(ParameterisedHandler, self.handler)
            return await takes_params(records, ctx, resource, params=params or {})
        return await self.handler(records, ctx, resource)

    def _handler_takes_params(self) -> bool:
        try:
            signature = inspect.signature(self.handler)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            # A callable object without an introspectable signature: assume the
            # old shape, which is the one that has always worked.
            return False
        parameters = signature.parameters
        if "params" in parameters:
            return True
        return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())

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
