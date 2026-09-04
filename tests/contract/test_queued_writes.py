"""Writes that are accepted but not applied.

A message published to a broker is not a saved record. These tests pin down
that the difference survives all the way to the caller, because the alternative
-- reporting success -- tells the user a change landed when it may never.
"""

from __future__ import annotations

import json

import pytest

from app.core.errors import ProviderError, UnsupportedOperation
from app.core.query import Condition, ListQuery, Op
from app.core.results import Ctx, Identity, WriteStatus
from app.providers.amqp import AMQPWriteProvider
from app.providers.base import Capabilities
from app.providers.composite import CompositeProvider, ReadOnly
from app.providers.memory import MemoryProvider
from app.providers.shim import CapabilityShim

CTX = Ctx(identity=Identity(subject="kim", email="kim@example.com", roles=frozenset({"user"})))


class FakeBroker:
    """Records what would have been published."""

    def __init__(self, *, fail: bool = False):
        self.published: list[tuple[str, dict, str]] = []
        self.fail = fail

    async def publish(self, routing_key, body, *, correlation_id):
        if self.fail:
            raise ConnectionError("broker unreachable")
        self.published.append((routing_key, body, correlation_id))

    async def health(self):
        return not self.fail, "fake broker"


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


@pytest.fixture
def publisher(broker) -> AMQPWriteProvider:
    return AMQPWriteProvider(broker, "deals")


class TestQueuedWrites:
    async def test_a_create_is_pending_not_ok(self, publisher):
        result = await publisher.create({"name": "New deal"}, CTX)
        assert result.status is WriteStatus.PENDING
        assert result.accepted, "the broker did take the message"
        assert not result.ok, "but nothing has been saved"

    async def test_pending_carries_a_reference(self, publisher):
        result = await publisher.create({"name": "x"}, CTX)
        assert result.correlation_id
        assert result.message

    async def test_the_message_names_the_operation_and_entity(self, publisher, broker):
        await publisher.update(7, {"stage": "won"}, CTX)
        routing_key, body, _ = broker.published[-1]
        assert routing_key == "deals.update"
        assert body["operation"] == "update"
        assert body["id"] == 7
        assert body["data"] == {"stage": "won"}

    async def test_the_message_says_who_asked(self, publisher, broker):
        # The consumer enforces its own rules; it must not have to trust the
        # message about who is acting.
        await publisher.create({"name": "x"}, CTX)
        _, body, _ = broker.published[-1]
        assert body["requested_by"]["email"] == "kim@example.com"

    async def test_values_are_serialised_for_the_wire(self, publisher, broker):
        from datetime import date
        from decimal import Decimal

        await publisher.create({"closed": date(2026, 4, 1), "amount": Decimal("10.50")}, CTX)
        _, body, _ = broker.published[-1]
        assert body["data"]["closed"] == "2026-04-01"
        assert body["data"]["amount"] == 10.5
        json.dumps(body)  # must round-trip

    async def test_a_failed_publish_is_an_error_not_a_pending(self):
        # Nothing was accepted, so reporting "queued" would be wrong.
        publisher = AMQPWriteProvider(FakeBroker(fail=True), "deals")
        with pytest.raises(ProviderError):
            await publisher.create({"name": "x"}, CTX)

    async def test_reading_is_refused_rather_than_faked(self, publisher):
        # An empty page would be a lie the shim could not correct.
        with pytest.raises(UnsupportedOperation, match="CompositeProvider"):
            await publisher.list(ListQuery(), CTX)

    def test_capabilities_declare_asynchronous_writes(self, publisher):
        assert publisher.capabilities.is_async_write
        assert not publisher.capabilities.read


class TestComposite:
    """Read from a store, write to a queue."""

    @pytest.fixture
    def composite(self, rows, broker):
        reader = MemoryProvider(rows, searchable_fields=("name", "email"))
        return CompositeProvider(reader, AMQPWriteProvider(broker, "deals"))

    async def test_reads_come_from_the_read_half(self, composite):
        page = await composite.list(ListQuery(page_size=50), CTX)
        assert page.total == 7

    async def test_reads_keep_the_read_half_query_support(self, composite):
        page = await composite.list(
            ListQuery(filter=Condition("stage", Op.EQ, "won"), page_size=50), CTX
        )
        assert len(page.items) == 3

    async def test_writes_go_to_the_write_half(self, composite, broker):
        result = await composite.update(1, {"stage": "won"}, CTX)
        assert result.status is WriteStatus.PENDING
        assert broker.published[-1][0] == "deals.update"

    async def test_a_write_does_not_change_the_read_side(self, composite):
        # The consumer applies it later; until then the store is unchanged.
        await composite.update(1, {"stage": "lost"}, CTX)
        record = await composite.get(1, CTX)
        assert record["stage"] == "won"

    def test_capabilities_combine_both_halves(self, composite):
        caps = composite.capabilities
        assert caps.read and caps.server_filter, "query support from the read half"
        assert caps.write and caps.is_async_write, "write behaviour from the write half"

    async def test_health_reports_both(self, composite):
        healthy, detail = await composite.health()
        assert healthy
        assert "read:" in detail and "write:" in detail

    async def test_the_shim_leaves_pending_writes_alone(self, rows, broker):
        # The shim must not "helpfully" turn a queued write into a success.
        composite = CompositeProvider(
            MemoryProvider(rows, capabilities=Capabilities(read=True)),
            AMQPWriteProvider(broker, "deals"),
        )
        shimmed = CapabilityShim(composite, search_fields=("name",))
        result = await shimmed.create({"name": "x"}, CTX)
        assert result.status is WriteStatus.PENDING


class TestReadOnlyWrapper:
    @pytest.fixture
    def locked(self, rows):
        return ReadOnly(MemoryProvider(rows), reason="Managed in the billing system.")

    async def test_reads_still_work(self, locked):
        assert (await locked.list(ListQuery(page_size=50), CTX)).total == 7

    async def test_writes_are_refused_with_the_stated_reason(self, locked):
        for call in (
            locked.create({"name": "x"}, CTX),
            locked.update(1, {"name": "x"}, CTX),
            locked.delete(1, CTX),
        ):
            with pytest.raises(UnsupportedOperation, match="billing system"):
                await call

    def test_capabilities_report_no_writes(self, locked):
        assert not locked.capabilities.writable
