"""Notifications.

The design point these tests pin down: a notification is *stored* first and
*delivered* second. The bell therefore always works, and a failing channel
loses nothing.
"""

from __future__ import annotations

from datetime import timedelta

from app.core.clock import utcnow
from app.core.results import Ctx, Identity
from app.notify.base import (
    BaseChannel,
    Delivery,
    Kind,
    Notification,
    Priority,
    assigned,
    build_channel,
    due,
    registered_channels,
)
from app.notify.channels import ConsoleChannel, EmailChannel, WebhookChannel
from app.notify.service import Notifier
from app.providers.memory import MemoryProvider

KIM = Identity(subject="3", email="kim@example.com", display_name="Kim", roles=frozenset())
CTX = Ctx(identity=KIM)


class Recorder(BaseChannel):
    """A channel that keeps what it was given."""

    name = "recorder"

    def __init__(self, *, fails: bool = False) -> None:
        self.sent: list[Notification] = []
        self.fails = fails

    async def send(self, notification: Notification) -> Delivery:
        if self.fails:
            raise RuntimeError("the channel is down")
        self.sent.append(notification)
        return Delivery(self.name, True, "recorded")


def store() -> MemoryProvider:
    return MemoryProvider([], searchable_fields=("title",))


def notifier_with(*channels, provider=None) -> Notifier:
    service = Notifier()
    service.background = False  # deliver inline so tests can assert
    service.bind(provider if provider is not None else store())
    service.use(list(channels))
    return service


class TestNotificationShape:
    def test_a_url_is_derived_from_the_record(self):
        n = Notification(recipient="a@b.test", title="x", resource="deals", record_id="7")
        assert n.url == "/r/deals/7"

    def test_an_explicit_url_is_kept(self):
        n = Notification(recipient="a@b.test", title="x", resource="deals",
                         record_id="7", url="/somewhere/else")
        assert n.url == "/somewhere/else"

    def test_a_future_one_is_scheduled(self):
        n = Notification(recipient="a@b.test", title="x",
                         due_at=utcnow() + timedelta(hours=1))
        assert n.is_scheduled

    def test_a_past_one_is_not(self):
        n = Notification(recipient="a@b.test", title="x",
                         due_at=utcnow() - timedelta(hours=1))
        assert not n.is_scheduled

    def test_the_helpers_build_sensible_notifications(self):
        a = assigned("a@b.test", "deals", 7, "the Acme renewal", actor="c@d.test")
        assert a.kind is Kind.ASSIGNED and a.url == "/r/deals/7"

        d = due("a@b.test", "activities", 3, "Call back", utcnow() + timedelta(days=1))
        assert d.kind is Kind.DUE and d.is_scheduled


class TestSending:
    async def test_a_notification_is_stored(self):
        provider = store()
        service = notifier_with(Recorder(), provider=provider)
        await service.send(Notification(recipient="a@b.test", title="Hello"), CTX)
        assert len(provider) == 1

    async def test_it_is_delivered_to_every_channel(self):
        first, second = Recorder(), ConsoleChannel()
        service = notifier_with(first, second)
        await service.send(Notification(recipient="a@b.test", title="Hello"), CTX)
        assert len(first.sent) == 1

    async def test_nobody_is_told_about_their_own_action(self):
        # A notification you caused is noise.
        recorder = Recorder()
        service = notifier_with(recorder)
        result = await service.send(
            Notification(recipient="kim@example.com", title="You did a thing",
                         actor="kim@example.com"),
            CTX,
        )
        assert result is None
        assert recorder.sent == []

    async def test_someone_else_is_told(self):
        recorder = Recorder()
        service = notifier_with(recorder)
        await service.send(
            Notification(recipient="sam@example.com", title="Kim did a thing",
                         actor="kim@example.com"),
            CTX,
        )
        assert len(recorder.sent) == 1

    async def test_a_notification_with_no_recipient_does_nothing(self):
        recorder = Recorder()
        service = notifier_with(recorder)
        assert await service.send(Notification(recipient="", title="x"), CTX) is None

    async def test_a_failing_channel_does_not_stop_the_others(self):
        broken, working = Recorder(fails=True), Recorder()
        service = notifier_with(broken, working)
        await service.send(Notification(recipient="a@b.test", title="Hello"), CTX)
        assert len(working.sent) == 1

    async def test_a_failing_channel_does_not_lose_the_notification(self):
        # The bell reads the stored row, so it works whatever the channels do.
        provider = store()
        service = notifier_with(Recorder(fails=True), provider=provider)
        await service.send(Notification(recipient="a@b.test", title="Hello"), CTX)
        assert len(provider) == 1

    async def test_a_scheduled_notification_is_not_delivered_yet(self):
        recorder = Recorder()
        service = notifier_with(recorder)
        await service.send(
            Notification(recipient="a@b.test", title="Later",
                         due_at=utcnow() + timedelta(days=1)),
            CTX,
        )
        assert recorder.sent == [], "a reminder is for later"


class TestReading:
    async def test_unread_notifications_are_returned(self):
        provider = store()
        service = notifier_with(Recorder(), provider=provider)
        await service.send(Notification(recipient="kim@example.com", title="One"), CTX)
        await service.send(Notification(recipient="kim@example.com", title="Two"), CTX)
        assert len(await service.unread_for(KIM, CTX)) == 2

    async def test_another_persons_notifications_are_not_returned(self):
        provider = store()
        service = notifier_with(Recorder(), provider=provider)
        await service.send(Notification(recipient="sam@example.com", title="Theirs"), CTX)
        assert await service.unread_for(KIM, CTX) == []

    async def test_a_scheduled_one_is_invisible_until_it_is_due(self):
        provider = store()
        service = notifier_with(Recorder(), provider=provider)
        await service.send(
            Notification(recipient="kim@example.com", title="Later",
                         due_at=utcnow() + timedelta(days=1)),
            CTX,
        )
        assert await service.unread_for(KIM, CTX) == []

    async def test_dismissing_removes_it_from_the_list(self):
        provider = store()
        service = notifier_with(Recorder(), provider=provider)
        record = await service.send(
            Notification(recipient="kim@example.com", title="One"), CTX
        )
        assert await service.mark_read(record.pk, KIM, CTX)
        assert await service.unread_for(KIM, CTX) == []

    async def test_one_person_cannot_dismiss_anothers(self):
        provider = store()
        service = notifier_with(Recorder(), provider=provider)
        record = await service.send(
            Notification(recipient="sam@example.com", title="Theirs"), CTX
        )
        assert not await service.mark_read(record.pk, KIM, CTX)

    async def test_clearing_all_dismisses_everything_of_theirs(self):
        provider = store()
        service = notifier_with(Recorder(), provider=provider)
        for i in range(3):
            await service.send(Notification(recipient="kim@example.com", title=str(i)), CTX)
        await service.send(Notification(recipient="sam@example.com", title="Theirs"), CTX)
        assert await service.mark_all_read(KIM, CTX) == 3
        assert len(await service.unread_for(KIM, CTX)) == 0


class TestScheduledDelivery:
    async def test_a_due_date_already_past_is_delivered_at_once(self):
        # "Due an hour ago" means now, not never.
        recorder = Recorder()
        service = notifier_with(recorder)
        await service.send(
            Notification(recipient="kim@example.com", title="Was due",
                         due_at=utcnow() - timedelta(minutes=5)),
            CTX,
        )
        assert len(recorder.sent) == 1

    async def test_the_sweep_delivers_a_reminder_once_it_comes_due(self):
        # The real sequence: scheduled for later, time passes, the sweep runs.
        provider = store()
        recorder = Recorder()
        service = notifier_with(recorder, provider=provider)
        record = await service.send(
            Notification(recipient="kim@example.com", title="Follow up",
                         due_at=utcnow() + timedelta(days=1)),
            CTX,
        )
        assert recorder.sent == [], "not delivered while it is still in the future"

        # Time passes.
        await provider.update(
            record.pk, {"due_at": utcnow() - timedelta(minutes=1)}, CTX
        )
        assert await service.deliver_due() == 1
        assert len(recorder.sent) == 1

    async def test_the_sweep_leaves_future_ones_alone(self):
        provider = store()
        service = notifier_with(Recorder(), provider=provider)
        await service.send(
            Notification(recipient="kim@example.com", title="Later",
                         due_at=utcnow() + timedelta(days=1)),
            CTX,
        )
        assert await service.deliver_due() == 0

    async def test_a_delivered_reminder_is_not_delivered_twice(self):
        provider = store()
        recorder = Recorder()
        service = notifier_with(recorder, provider=provider)
        await service.send(
            Notification(recipient="kim@example.com", title="Due",
                         due_at=utcnow() - timedelta(minutes=1)),
            CTX,
        )
        await service.deliver_due()
        await service.deliver_due()
        assert len(recorder.sent) == 1


class TestConcurrentSweeps:
    """Two schedulers must not deliver the same reminder twice.

    The scenario this guards is ordinary rather than exotic: a sweep running on
    two hosts, or one that overruns its own interval and overlaps itself.
    """

    async def test_two_sweeps_at_once_deliver_once(self):
        import asyncio

        provider = store()
        recorder = Recorder()
        service = notifier_with(recorder, provider=provider)
        # Scheduled ahead and then moved back, because a notification created
        # already-due is delivered on the spot and never reaches the sweep.
        for i in range(5):
            record = await service.send(
                Notification(recipient="kim@example.com", title=f"Due {i}",
                             due_at=utcnow() + timedelta(days=1)),
                CTX,
            )
            await provider.update(
                record.pk, {"due_at": utcnow() - timedelta(minutes=1)}, CTX
            )

        delivered = await asyncio.gather(service.deliver_due(), service.deliver_due())
        # Between them they handle each reminder exactly once.
        assert sum(delivered) == 5
        assert len(recorder.sent) == 5
        assert len({n.title for n in recorder.sent}) == 5


class TestDeliveryRetries:
    """Retry what may succeed; do not retry what will not."""

    async def test_a_transient_failure_is_retried(self):
        from app.notify.base import Delivery, with_retries

        attempts = []

        async def flaky() -> Delivery:
            attempts.append(1)
            if len(attempts) < 3:
                return Delivery("test", False, "timeout", transient=True)
            return Delivery("test", True, "ok")

        result = await with_retries(flaky, base_delay=0)
        assert result.ok and len(attempts) == 3

    async def test_a_permanent_failure_is_not_retried(self):
        # The point of the distinction: sending a rejected address three times
        # produces the same rejection three times and delays the report.
        from app.notify.base import Delivery, with_retries

        attempts = []

        async def rejected() -> Delivery:
            attempts.append(1)
            return Delivery("test", False, "550 no such mailbox")

        result = await with_retries(rejected, base_delay=0)
        assert not result.ok and len(attempts) == 1

    async def test_retries_give_up_eventually(self):
        from app.notify.base import Delivery, with_retries

        attempts = []

        async def always_down() -> Delivery:
            attempts.append(1)
            return Delivery("test", False, "connection refused", transient=True)

        result = await with_retries(always_down, attempts=3, base_delay=0)
        assert not result.ok and len(attempts) == 3


class TestShutdown:
    async def test_in_flight_deliveries_are_awaited(self):
        """A deploy must not cancel a delivery half-way.

        The row is written before delivery is attempted, so a cancelled
        delivery loses the outbound copy *and* leaves a row claiming it was
        never sent.
        """
        import asyncio

        class Slow(Recorder):
            async def send(self, notification):
                await asyncio.sleep(0.05)
                return await super().send(notification)

        recorder = Slow()
        provider = store()
        service = notifier_with(recorder, provider=provider)
        service.background = True
        await service.send(Notification(recipient="kim@example.com", title="Slow"), CTX)
        assert recorder.sent == [], "delivery has not finished yet"

        assert await service.drain(timeout=2) == 1
        assert len(recorder.sent) == 1


class TestChannelSelection:
    def test_a_channel_ignores_what_is_below_its_threshold(self):
        channel = EmailChannel(min_priority="high")
        assert not channel.wants(Notification(recipient="a@b.test", title="x",
                                              priority=Priority.NORMAL))
        assert channel.wants(Notification(recipient="a@b.test", title="x",
                                          priority=Priority.URGENT))

    def test_a_notification_can_name_its_channels(self):
        recorder = Recorder()
        assert recorder.wants(
            Notification(recipient="a@b.test", title="x", channels=("recorder",))
        )
        assert not recorder.wants(
            Notification(recipient="a@b.test", title="x", channels=("email",))
        )


class TestChannelImplementations:
    def test_the_builtin_channels_are_registered(self):
        assert {"inapp", "email", "webhook", "console"} <= set(registered_channels())

    def test_an_email_is_composed_with_a_link(self):
        channel = EmailChannel(sender="crm@x.test", base_url="https://crm.x.test")
        message = channel.build_message(
            Notification(recipient="a@b.test", title="A deal moved",
                         body="Details here", resource="deals", record_id="7")
        )
        assert message["To"] == "a@b.test"
        assert message["Subject"] == "A deal moved"
        assert "https://crm.x.test/r/deals/7" in message.get_content()

    async def test_email_declines_a_recipient_that_is_not_an_address(self):
        # Recipients are identities, which are usually emails but need not be.
        channel = EmailChannel()
        result = await channel.send(Notification(recipient="user-42", title="x"))
        assert not result.delivered

    def test_the_slack_payload_is_a_text_field(self):
        channel = WebhookChannel(url="https://hooks.test/x", style="slack",
                                 base_url="https://crm.x.test")
        payload = channel.payload(
            Notification(recipient="a@b.test", title="Hello", resource="deals", record_id="1")
        )
        assert "text" in payload and "Hello" in payload["text"]

    def test_the_raw_payload_carries_the_fields(self):
        channel = WebhookChannel(url="https://hooks.test/x", style="raw")
        payload = channel.payload(Notification(recipient="a@b.test", title="Hello"))
        assert payload["title"] == "Hello" and payload["recipient"] == "a@b.test"

    async def test_a_webhook_without_a_url_reports_rather_than_raises(self):
        result = await WebhookChannel().send(Notification(recipient="a@b.test", title="x"))
        assert not result.delivered and "URL" in result.detail

    def test_a_channel_is_built_by_name(self):
        assert isinstance(build_channel("console"), ConsoleChannel)


class TestWithoutAStore:
    async def test_delivery_still_happens_with_nothing_to_store_into(self):
        service = Notifier()
        service.background = False
        recorder = Recorder()
        service.use([recorder])
        await service.send(Notification(recipient="a@b.test", title="Hello"), CTX)
        assert len(recorder.sent) == 1
