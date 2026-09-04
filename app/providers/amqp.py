"""A provider that writes by publishing messages.

This is the case that shaped ``WriteResult``. Publishing a message is not a
write in the usual sense: the broker confirms it took the message, nothing
more. Whether the change is ever applied happens elsewhere, later, and may
fail without this process hearing about it.

So writes here return ``PENDING``, and the interface refuses to pretend
otherwise. The UI shows "queued" rather than a saved record, which is the
truth. Pair with :class:`~app.providers.composite.CompositeProvider` to read
from a database while writing to a queue.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from app.core.clock import utcnow
from app.core.connections import ConnectionSpec, register_connection
from app.core.errors import ProviderError, UnsupportedOperation
from app.core.query import ListQuery, Page
from app.core.registry import register_provider_factory
from app.core.results import Ctx, Record, WriteResult
from app.providers.base import BaseProvider, Capabilities


def _encode(value: Any) -> Any:
    """Make a canonical Python value safe to put in a JSON message body."""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_encode(v) for v in value]
    return value


class AMQPConnection:
    """A broker connection and one channel, opened lazily."""

    def __init__(self, url: str, *, exchange: str = "", durable: bool = True) -> None:
        self.url = url
        self.exchange_name = exchange
        self.durable = durable
        self._connection: Any = None
        self._channel: Any = None
        self._exchange: Any = None

    async def channel(self) -> Any:
        if self._channel is not None and not self._channel.is_closed:
            return self._channel
        import aio_pika

        self._connection = await aio_pika.connect_robust(self.url)
        self._channel = await self._connection.channel()
        if self.exchange_name:
            self._exchange = await self._channel.declare_exchange(
                self.exchange_name, aio_pika.ExchangeType.TOPIC, durable=self.durable
            )
        else:
            self._exchange = self._channel.default_exchange
        return self._channel

    async def exchange(self) -> Any:
        await self.channel()
        return self._exchange

    async def publish(self, routing_key: str, body: dict[str, Any], *, correlation_id: str) -> None:
        import aio_pika

        exchange = await self.exchange()
        message = aio_pika.Message(
            body=json.dumps(body).encode(),
            content_type="application/json",
            correlation_id=correlation_id,
            delivery_mode=(
                aio_pika.DeliveryMode.PERSISTENT if self.durable else aio_pika.DeliveryMode.NOT_PERSISTENT
            ),
        )
        await exchange.publish(message, routing_key=routing_key)

    async def close(self) -> None:
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()
        self._connection = self._channel = self._exchange = None

    async def health(self) -> tuple[bool, str]:
        try:
            await self.channel()
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        target = self.exchange_name or "(default exchange)"
        return True, f"connected, publishing to {target}"


@register_connection(
    "amqp",
    close=lambda conn: conn.close(),
    check=lambda conn: conn.health(),
)
async def open_amqp(spec: ConnectionSpec) -> AMQPConnection:
    return AMQPConnection(
        str(spec.option("url", required=True)),
        exchange=str(spec.option("exchange", "") or ""),
        durable=spec.flag("durable", True),
    )


class AMQPWriteProvider(BaseProvider):
    """Turns writes into published commands.

    Reads are refused outright rather than faked. A queue is not a store, and
    a provider that answered ``list`` with an empty page would be lying in a
    way the shim could not correct.
    """

    def __init__(
        self,
        connection: AMQPConnection,
        entity: str,
        *,
        name: str = "",
        pk_field: str = "id",
        routing_template: str = "{entity}.{operation}",
        include_identity: bool = True,
    ) -> None:
        self.connection = connection
        self.entity = entity
        self.name = name or f"amqp:{entity}"
        self.pk_field = pk_field
        self.routing_template = routing_template
        self.include_identity = include_identity
        self.capabilities = Capabilities(
            read=False,
            write=True,
            delete=True,
            # Writes are accepted, never confirmed.
            write_mode="async",
        )

    def routing_key(self, operation: str) -> str:
        return self.routing_template.format(entity=self.entity, operation=operation)

    async def _publish(
        self, operation: str, ctx: Ctx, *, pk: Any = None, data: dict[str, Any] | None = None
    ) -> WriteResult:
        correlation_id = uuid.uuid4().hex
        body = {
            "operation": operation,
            "entity": self.entity,
            "id": pk,
            "data": {k: _encode(v) for k, v in (data or {}).items()},
            "correlation_id": correlation_id,
            "requested_at": utcnow().isoformat(),
        }
        if self.include_identity:
            # Who asked for the change, so the consumer can enforce its own
            # rules rather than trusting the message.
            body["requested_by"] = {
                "subject": ctx.identity.subject,
                "email": ctx.identity.email,
                "roles": sorted(ctx.identity.roles),
            }

        try:
            await self.connection.publish(self.routing_key(operation), body, correlation_id=correlation_id)
        except Exception as exc:
            # Publishing failed, so nothing was accepted. This is a real error,
            # distinct from the ordinary "accepted but unconfirmed" outcome.
            raise ProviderError(f"{self.name}: could not publish the command: {exc}") from exc

        record = Record({**(data or {}), self.pk_field: pk}, self.pk_field) if pk is not None else None
        return WriteResult.pending(
            record=record,
            message=f"The {operation} request has been queued.",
            correlation_id=correlation_id,
        )

    async def create(self, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        return await self._publish("create", ctx, data=data)

    async def update(self, pk: Any, data: dict[str, Any], ctx: Ctx) -> WriteResult:
        return await self._publish("update", ctx, pk=pk, data=data)

    async def delete(self, pk: Any, ctx: Ctx) -> WriteResult:
        return await self._publish("delete", ctx, pk=pk)

    async def list(self, q: ListQuery, ctx: Ctx) -> Page[Record]:
        raise UnsupportedOperation(
            f"{self.name} publishes commands and cannot be read from. "
            f"Combine it with a readable provider using CompositeProvider."
        )

    async def get(self, pk: Any, ctx: Ctx) -> Record | None:
        raise UnsupportedOperation(f"{self.name} cannot be read from")

    async def health(self) -> tuple[bool, str]:
        return await self.connection.health()

    async def close(self) -> None:
        return None  # the connection registry owns the broker connection


@register_provider_factory("amqp")
def build_amqp_provider(handle: AMQPConnection, target: str, resource) -> AMQPWriteProvider:
    return AMQPWriteProvider(handle, target, pk_field=resource.pk)
