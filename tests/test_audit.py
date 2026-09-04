"""The audit trail.

Implemented as a provider wrapper rather than as calls in the routes, so these
tests exercise the wrapper directly -- if it records a write here, it records
that write wherever it originated.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from app.core.errors import NotFound
from app.core.query import ListQuery
from app.core.results import Ctx, Identity, WriteResult
from app.providers.audit import REDACTED, AuditingProvider, AuditSink, diff
from app.providers.base import Capabilities
from app.providers.memory import MemoryProvider

KIM = Identity(subject="3", email="kim@example.com", display_name="Kim Alvarez",
               roles=frozenset({"user"}), provider="local")
CTX = Ctx(identity=KIM, request_id="req-123", extra={"ip": "10.0.0.9"})


class CapturingSink(AuditSink):
    """Keeps entries in memory instead of writing them anywhere."""

    def __init__(self) -> None:
        super().__init__(None)
        self.entries: list[dict] = []

    async def write(self, entry: dict) -> None:
        self.entries.append(entry)

    @property
    def last(self) -> dict:
        assert self.entries, "no audit entry was written"
        return self.entries[-1]

    def changes(self, index: int = -1) -> dict:
        raw = self.entries[index]["changes"]
        return json.loads(raw) if raw else {}


@pytest.fixture
def sink() -> CapturingSink:
    return CapturingSink()


@pytest.fixture
def provider(rows, sink):
    return AuditingProvider(
        MemoryProvider(rows), "records", label_field="name", audit_sink=sink
    )


class TestDiff:
    def test_only_changed_fields_are_reported(self):
        # A form resubmits everything; a log saying "14 fields changed" when one
        # did is a log nobody reads.
        result = diff({"a": 1, "b": 2, "c": 3}, {"a": 1, "b": 99, "c": 3})
        assert result == {"b": [2, 99]}

    def test_a_new_value_is_reported_against_nothing(self):
        assert diff(None, {"a": 1}) == {"a": [None, 1]}

    def test_equivalent_values_of_different_types_are_not_a_change(self):
        # A driver may return a date as a string; that is not an edit.
        assert diff({"d": date(2026, 3, 4)}, {"d": "2026-03-04"}) == {}

    def test_decimals_are_comparable_with_floats(self):
        assert diff({"amount": Decimal("10.50")}, {"amount": 10.5}) == {}

    def test_secrets_are_never_written_out(self):
        result = diff({"password_hash": "old"}, {"password_hash": "new"})
        assert result == {"password_hash": [REDACTED, REDACTED]}

    @pytest.mark.parametrize("name", ["password", "token", "api_key", "secret"])
    def test_every_sensitive_name_is_redacted(self, name):
        assert diff({name: "a"}, {name: "b"})[name] == [REDACTED, REDACTED]


class TestWritesAreRecorded:
    async def test_a_create_is_recorded(self, provider, sink):
        await provider.create({"name": "Sophie Wilson", "amount": 400}, CTX)
        assert sink.last["action"] == "create"
        assert sink.last["resource"] == "records"
        assert sink.changes()["name"] == [None, "Sophie Wilson"]

    async def test_an_update_records_the_before_and_after(self, provider, sink):
        await provider.update(1, {"stage": "lost"}, CTX)
        assert sink.changes()["stage"] == ["won", "lost"]

    async def test_an_update_records_only_what_changed(self, provider, sink):
        await provider.update(1, {"stage": "won", "owner": "sam"}, CTX)
        # stage was already "won".
        assert set(sink.changes()) == {"owner"}

    async def test_a_write_that_changes_nothing_records_no_changes(self, provider, sink):
        await provider.update(1, {"stage": "won"}, CTX)
        assert sink.changes() == {}
        assert sink.last["action"] == "update", "the attempt is still recorded"

    async def test_a_delete_records_the_whole_record(self, provider, sink):
        await provider.delete(1, CTX)
        changes = sink.changes()
        assert changes["name"] == ["Ada Lovelace", None]
        assert sink.last["action"] == "delete"

    async def test_the_record_is_identified(self, provider, sink):
        await provider.update(2, {"stage": "lost"}, CTX)
        assert sink.last["record_id"] == "2"
        assert sink.last["record_label"] == "Grace Hopper"


class TestActorIsRecorded:
    async def test_who_made_the_change(self, provider, sink):
        await provider.update(1, {"stage": "lost"}, CTX)
        assert sink.last["actor"] == "kim@example.com"
        assert sink.last["actor_name"] == "Kim Alvarez"

    async def test_how_they_signed_in(self, provider, sink):
        await provider.update(1, {"stage": "lost"}, CTX)
        assert sink.last["actor_provider"] == "local"

    async def test_the_request_and_address_are_kept(self, provider, sink):
        await provider.update(1, {"stage": "lost"}, CTX)
        assert sink.last["request_id"] == "req-123"
        assert sink.last["ip"] == "10.0.0.9"


class TestOutcomeIsRecordedHonestly:
    async def test_a_queued_write_is_not_recorded_as_applied(self, sink):
        class Queue(MemoryProvider):
            async def update(self, pk, data, ctx):
                return WriteResult.pending(message="Sent to the queue.")

        provider = AuditingProvider(
            Queue([{"id": 1, "name": "x"}]), "records", audit_sink=sink
        )
        await provider.update(1, {"name": "y"}, CTX)
        assert sink.last["status"] == "pending"
        assert sink.last["detail"] == "Sent to the queue."

    async def test_a_rejected_write_is_recorded_as_failed(self, sink):
        class Refuses(MemoryProvider):
            async def update(self, pk, data, ctx):
                return WriteResult.failure("The backend said no.")

        provider = AuditingProvider(
            Refuses([{"id": 1, "name": "x"}]), "records", audit_sink=sink
        )
        await provider.update(1, {"name": "y"}, CTX)
        assert sink.last["status"] == "error"


class TestAuditingNeverBreaksTheOperation:
    async def test_a_failing_sink_does_not_fail_the_write(self, rows):
        class Broken(AuditSink):
            async def write(self, entry):
                raise RuntimeError("the log is on fire")

        provider = AuditingProvider(MemoryProvider(rows), "records", audit_sink=Broken())
        # The sink swallows and logs its own failure; the write must still land.
        result = await provider.update(1, {"stage": "lost"}, CTX)
        assert result.ok
        assert (await provider.get(1, CTX))["stage"] == "lost"

    async def test_a_write_only_backend_is_still_audited(self, sink):
        # Nothing to read before the change, so the entry records the request
        # rather than the difference. That is better than no entry.
        class WriteOnly(MemoryProvider):
            async def get(self, pk, ctx):
                raise NotImplementedError("this backend cannot be read")

        provider = AuditingProvider(
            WriteOnly([{"id": 1, "name": "x"}]), "records", audit_sink=sink
        )
        await provider.update(1, {"name": "y"}, CTX)
        assert sink.last["action"] == "update"

    async def test_an_error_from_the_backend_still_propagates(self, provider):
        with pytest.raises(NotFound):
            await provider.update(999, {"stage": "won"}, CTX)


class TestReadsAreUntouched:
    async def test_listing_writes_no_entry(self, provider, sink):
        await provider.list(ListQuery(page_size=50), CTX)
        assert sink.entries == []

    async def test_reading_one_record_writes_no_entry(self, provider, sink):
        await provider.get(1, CTX)
        assert sink.entries == []

    async def test_capabilities_pass_through_unchanged(self, rows, sink):
        inner = MemoryProvider(rows, capabilities=Capabilities(read=True, write=True))
        provider = AuditingProvider(inner, "records", audit_sink=sink)
        assert provider.capabilities is inner.capabilities


class TestUnconfiguredSink:
    async def test_writes_still_work_without_a_sink(self, rows):
        # A deployment with no audit_log resource must not break every write.
        provider = AuditingProvider(MemoryProvider(rows), "records", audit_sink=AuditSink())
        assert (await provider.update(1, {"stage": "lost"}, CTX)).ok
