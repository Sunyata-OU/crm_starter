"""The durable job queue.

The properties worth pinning are the ones that only show up when something goes
wrong: two workers racing for the same job, a worker that dies mid-job, a
handler that fails in a way retrying cannot fix, and a job whose handler has
not been deployed yet. Each of those has a wrong behaviour that looks fine in
development -- run it twice, lose it, retry for ever, drop it -- so each has a
test.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.core.clock import utcnow
from app.core.errors import UnsupportedOperation
from app.core.query import ListQuery
from app.core.results import Ctx
from app.jobs import base as jobs_base
from app.jobs.base import Job, Retry, backoff
from app.jobs.queue import JobQueue
from app.providers.memory import MemoryProvider

CTX = Ctx.system()

JOB_COLUMNS = {
    "kind": "", "payload": "{}", "status": "queued", "run_at": None,
    "attempts": 0, "max_attempts": 5, "claimed_by": None, "claimed_at": None,
    "finished_at": None, "last_error": None, "key": None, "priority": 0,
}


@pytest.fixture(autouse=True)
def clean_handlers():
    """Handlers are process-wide, so a test must not leak one into the next."""
    saved = dict(jobs_base._HANDLERS)
    jobs_base.clear_handlers()
    yield
    jobs_base._HANDLERS.clear()
    jobs_base._HANDLERS.update(saved)


@pytest.fixture
def queue():
    q = JobQueue()
    q.bind(MemoryProvider([], name="jobs"))
    q.name = "test-worker"
    return q


async def _row(queue: JobQueue, pk):
    return await queue.provider.get(pk, CTX)


class TestEnqueueing:
    @pytest.mark.asyncio
    async def test_a_job_is_a_row_before_anything_runs(self, queue):
        """The whole point: accepted work exists before it is done."""
        record = await queue.enqueue("thing", {"a": 1})
        assert record is not None
        stored = await _row(queue, record.pk)
        assert stored["status"] == "queued"
        assert stored["kind"] == "thing"

    @pytest.mark.asyncio
    async def test_the_payload_survives_a_round_trip(self, queue):
        record = await queue.enqueue("thing", {"a": 1, "b": ["x", None]})
        job = Job.from_row(await _row(queue, record.pk))
        assert job.payload == {"a": 1, "b": ["x", None]}

    @pytest.mark.asyncio
    async def test_a_key_makes_enqueueing_idempotent(self, queue):
        first = await queue.enqueue("thing", key="once")
        second = await queue.enqueue("thing", key="once")
        assert first.pk == second.pk

    @pytest.mark.asyncio
    async def test_a_key_does_not_block_requeueing_finished_work(self, queue):
        """A repeat of work that has already run is a new request, not a dupe."""
        first = await queue.enqueue("thing", key="nightly")
        await queue.provider.update(first.pk, {"status": "done"}, CTX)
        second = await queue.enqueue("thing", key="nightly")
        assert second.pk != first.pk

    @pytest.mark.asyncio
    async def test_a_delay_is_a_later_run_at_not_a_sleeping_task(self, queue):
        record = await queue.enqueue("thing", delay=60)
        assert (await _row(queue, record.pk))["run_at"] > utcnow()

    @pytest.mark.asyncio
    async def test_enqueueing_without_a_queue_says_so_rather_than_pretending(self):
        unbound = JobQueue()
        assert await unbound.enqueue("thing") is None

    @pytest.mark.asyncio
    async def test_enqueue_or_run_falls_back_to_running_it(self):
        ran: list[Job] = []

        @jobs_base.register("thing")
        async def handler(job):
            ran.append(job)

        unbound = JobQueue()
        queued = await unbound.enqueue_or_run("thing", {"a": 1})
        assert queued is False
        assert ran and ran[0].payload == {"a": 1}


class TestClaiming:
    @pytest.mark.asyncio
    async def test_a_job_is_claimed_once_even_by_two_workers(self, queue):
        """The property everything else rests on."""

        @jobs_base.register("thing")
        async def handler(job):
            pass

        await queue.enqueue("thing")

        other = JobQueue()
        other.bind(queue.provider)
        other.name = "other-worker"

        mine, theirs = await asyncio.gather(queue.claim(5), other.claim(5))
        assert len(mine) + len(theirs) == 1, "the same job was claimed twice"

    @pytest.mark.asyncio
    async def test_a_job_not_yet_due_is_not_claimed(self, queue):
        @jobs_base.register("thing")
        async def handler(job):
            pass

        await queue.enqueue("thing", delay=3600)
        assert await queue.claim(5) == []

    @pytest.mark.asyncio
    async def test_higher_priority_goes_first(self, queue):
        @jobs_base.register("thing")
        async def handler(job):
            pass

        await queue.enqueue("thing", {"n": 1}, priority=0)
        await queue.enqueue("thing", {"n": 2}, priority=10)
        claimed = await queue.claim(1)
        assert claimed[0].payload == {"n": 2}

    @pytest.mark.asyncio
    async def test_the_attempt_is_counted_at_the_claim(self, queue):
        """A worker killed mid-job has still used an attempt.

        Counting at the failure instead would let a job that reliably kills its
        worker run for ever.
        """

        @jobs_base.register("thing")
        async def handler(job):
            pass

        record = await queue.enqueue("thing")
        await queue.claim(1)
        assert (await _row(queue, record.pk))["attempts"] == 1

    @pytest.mark.asyncio
    async def test_a_job_with_no_handler_is_left_queued_not_failed(self, queue):
        """Its handler may be one deploy away."""
        record = await queue.enqueue("not.deployed.yet")
        assert await queue.claim(5) == []
        assert (await _row(queue, record.pk))["status"] == "queued"
        assert (await _row(queue, record.pk))["attempts"] == 0

    @pytest.mark.asyncio
    async def test_a_provider_that_cannot_claim_says_so_once(self, queue, caplog):
        """Degrades loudly: one worker is a fine arrangement, silence is not."""

        class NoConditionalWrites(MemoryProvider):
            async def update_if(self, pk, data, expect, ctx):
                raise UnsupportedOperation("no")

        queue.bind(NoConditionalWrites([], name="jobs"))

        @jobs_base.register("thing")
        async def handler(job):
            pass

        await queue.enqueue("thing")
        await queue.enqueue("thing")
        with caplog.at_level("WARNING"):
            await queue.claim(5)
        warnings = [r for r in caplog.records if "cannot claim rows atomically" in r.message]
        assert len(warnings) == 1


class TestRunning:
    @pytest.mark.asyncio
    async def test_a_successful_job_is_recorded_done(self, queue):
        @jobs_base.register("thing")
        async def handler(job):
            pass

        record = await queue.enqueue("thing")
        assert await queue.run_once(1) == 1
        stored = await _row(queue, record.pk)
        assert stored["status"] == "done"
        assert stored["finished_at"] is not None

    @pytest.mark.asyncio
    async def test_the_handler_gets_the_payload(self, queue):
        seen = {}

        @jobs_base.register("thing")
        async def handler(job):
            seen.update(job.payload)

        await queue.enqueue("thing", {"who": "ada"})
        await queue.run_once(1)
        assert seen == {"who": "ada"}

    @pytest.mark.asyncio
    async def test_a_plain_exception_fails_without_retrying(self, queue):
        """Repeating a bug produces the same bug and hides it for longer."""
        calls = []

        @jobs_base.register("thing")
        async def handler(job):
            calls.append(1)
            raise KeyError("bad payload")

        record = await queue.enqueue("thing")
        await queue.run_once(1)
        stored = await _row(queue, record.pk)
        assert stored["status"] == "failed"
        assert "KeyError" in stored["last_error"]

        # And it is not picked up again.
        assert await queue.run_once(1) == 0
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_a_retry_goes_back_on_the_queue_for_later(self, queue):
        @jobs_base.register("thing")
        async def handler(job):
            raise Retry("upstream is busy")

        record = await queue.enqueue("thing")
        await queue.run_once(1)
        stored = await _row(queue, record.pk)
        assert stored["status"] == "queued"
        assert stored["run_at"] > utcnow()
        assert stored["last_error"] == "upstream is busy"

    @pytest.mark.asyncio
    async def test_a_retry_may_name_its_own_delay(self, queue):
        @jobs_base.register("thing")
        async def handler(job):
            raise Retry("rate limited", delay=0)

        record = await queue.enqueue("thing")
        await queue.run_once(1)
        assert (await _row(queue, record.pk))["run_at"] <= utcnow()

    @pytest.mark.asyncio
    async def test_retries_stop_at_the_attempt_limit(self, queue):
        attempts = []

        @jobs_base.register("thing")
        async def handler(job):
            attempts.append(job.attempts)
            raise Retry("still busy", delay=0)

        record = await queue.enqueue("thing", max_attempts=3)
        for _ in range(5):
            await queue.run_once(1)

        stored = await _row(queue, record.pk)
        assert stored["status"] == "failed"
        assert attempts == [1, 2, 3], "a job must not retry past its limit"

    @pytest.mark.asyncio
    async def test_a_cancelled_job_is_done_not_failed(self, queue):
        from app.jobs.base import Cancelled

        @jobs_base.register("thing")
        async def handler(job):
            raise Cancelled("the record is gone")

        record = await queue.enqueue("thing")
        await queue.run_once(1)
        stored = await _row(queue, record.pk)
        assert stored["status"] == "done"
        assert "the record is gone" in stored["last_error"]

    @pytest.mark.asyncio
    async def test_a_job_that_overruns_is_cancelled_and_retried(self, queue):
        @jobs_base.register("thing")
        async def handler(job):
            await asyncio.sleep(5)

        queue.job_timeout = 0.01
        record = await queue.enqueue("thing")
        await queue.run_once(1)
        stored = await _row(queue, record.pk)
        assert stored["status"] == "queued"
        assert "timed out" in stored["last_error"]


class TestAbandonedJobs:
    @pytest.mark.asyncio
    async def test_a_job_whose_worker_vanished_is_requeued(self, queue):
        """The failure a task on an event loop cannot recover from."""
        record = await queue.enqueue("thing")
        await queue.provider.update(
            record.pk,
            {
                "status": "running",
                "claimed_by": "a-worker-that-died",
                "claimed_at": utcnow() - timedelta(hours=2),
                "attempts": 1,
            },
            CTX,
        )

        assert await queue.release_stale() == 1
        stored = await _row(queue, record.pk)
        assert stored["status"] == "queued"
        assert stored["claimed_by"] is None
        assert "stopped without finishing" in stored["last_error"]

    @pytest.mark.asyncio
    async def test_a_job_still_within_its_lease_is_left_alone(self, queue):
        record = await queue.enqueue("thing")
        await queue.provider.update(
            record.pk,
            {"status": "running", "claimed_by": "busy", "claimed_at": utcnow()},
            CTX,
        )
        assert await queue.release_stale() == 0
        assert (await _row(queue, record.pk))["status"] == "running"

    @pytest.mark.asyncio
    async def test_a_job_that_kills_every_worker_is_eventually_parked(self, queue):
        """Otherwise one bad payload loops for ever and takes the queue with it."""
        record = await queue.enqueue("thing", max_attempts=2)
        await queue.provider.update(
            record.pk,
            {
                "status": "running",
                "claimed_by": "victim",
                "claimed_at": utcnow() - timedelta(hours=2),
                "attempts": 2,
            },
            CTX,
        )
        await queue.release_stale()
        assert (await _row(queue, record.pk))["status"] == "failed"


class TestTheLoop:
    @pytest.mark.asyncio
    async def test_it_drains_what_is_there_and_stops(self, queue):
        done = []

        @jobs_base.register("thing")
        async def handler(job):
            done.append(job.payload["n"])

        for n in range(5):
            await queue.enqueue("thing", {"n": n})

        assert await queue.work(concurrency=2, poll=0.01, max_jobs=5) == 5
        assert sorted(done) == [0, 1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_stop_wakes_it_from_an_idle_poll(self, queue):
        """A shutdown must not wait out the poll interval."""

        async def stop_soon():
            await asyncio.sleep(0.01)
            queue.stop()

        async with asyncio.timeout(2):
            await asyncio.gather(queue.work(poll=30.0), stop_soon())


class TestReporting:
    @pytest.mark.asyncio
    async def test_counts_group_by_state(self, queue):
        @jobs_base.register("thing")
        async def handler(job):
            pass

        await queue.enqueue("thing")
        await queue.enqueue("thing", delay=3600)
        await queue.run_once(1)
        assert await queue.counts() == {"done": 1, "queued": 1}

    @pytest.mark.asyncio
    async def test_retrying_a_failed_job_resets_its_attempts(self, queue):
        """A job is unretryable exactly when you have fixed why it failed."""

        @jobs_base.register("thing")
        async def handler(job):
            raise ValueError("nope")

        record = await queue.enqueue("thing")
        await queue.run_once(1)
        assert (await _row(queue, record.pk))["status"] == "failed"

        await queue.retry(record.pk)
        stored = await _row(queue, record.pk)
        assert stored["status"] == "queued"
        assert stored["attempts"] == 0


class TestBackoff:
    def test_it_doubles(self):
        assert backoff(1) == 30.0
        assert backoff(2) == 60.0
        assert backoff(3) == 120.0

    def test_it_is_capped(self):
        assert backoff(50) == 3600.0


class TestQueuedNotificationDelivery:
    """The seam: delivery moved off the event loop and into the table.

    These bind the *process-wide* queue rather than a fresh one, because that
    is what the notifier reaches for -- and a test that wires a different
    object would pass while the real path stayed broken.
    """

    @pytest.fixture
    def wired(self):
        from app.jobs import handlers as job_handlers
        from app.jobs import queue as global_queue
        from app.notify import notifier
        from app.notify.base import Delivery

        # Importing the module registers the handler, but only the first time:
        # the autouse fixture above has cleared the registry, so put it back.
        if jobs_base.handler_for(job_handlers.NOTIFY_DELIVER) is None:
            jobs_base.register(job_handlers.NOTIFY_DELIVER)(job_handlers.deliver_notification)

        sent: list[str] = []

        class Channel:
            name = "test"

            def wants(self, notification):
                return True

            async def send(self, notification):
                sent.append(notification.title)
                return Delivery(self.name, True)

            async def health(self):
                return True, "ok"

        saved_queue = global_queue.provider
        saved = (
            notifier.provider,
            list(notifier.channels),
            notifier.background,
            notifier.queue_deliveries,
        )
        global_queue.bind(MemoryProvider([], name="jobs"))
        notifier.bind(MemoryProvider([], name="notifications"))
        notifier.use([Channel()])
        notifier.background = False
        notifier.queue_deliveries = True
        try:
            yield global_queue, notifier, sent
        finally:
            global_queue.provider = saved_queue
            notifier.provider, saved_channels, notifier.background, notifier.queue_deliveries = saved
            notifier.use(saved_channels)

    @pytest.mark.asyncio
    async def test_a_notification_is_stored_and_queued_not_sent(self, wired):
        from app.notify.base import Notification

        queue, notifier, sent = wired
        record = await notifier.send(Notification(recipient="ada@example.com", title="Hello"))

        assert record is not None, "the notification itself must still be stored"
        assert sent == [], "delivery must not have happened yet"
        assert await queue.counts() == {"queued": 1}

    @pytest.mark.asyncio
    async def test_the_worker_delivers_it(self, wired):
        from app.notify.base import Notification

        queue, notifier, sent = wired
        await notifier.send(Notification(recipient="ada@example.com", title="Hello"))

        notifier.queue_deliveries = False  # the worker delivers; it does not requeue
        assert await queue.run_once(1) == 1
        assert sent == ["Hello"]

    @pytest.mark.asyncio
    async def test_delivering_twice_is_declined(self, wired):
        """At-least-once means a job can run again. It must not send again."""
        from app.notify.base import Notification

        queue, notifier, sent = wired
        await notifier.send(Notification(recipient="ada@example.com", title="Hello"))
        notifier.queue_deliveries = False
        await queue.run_once(1)

        job = (await queue.provider.list(ListQuery(), CTX)).items[0]
        await queue.retry(job.pk)
        await queue.run_once(1)
        assert sent == ["Hello"], "a re-run job re-sent the notification"

    @pytest.mark.asyncio
    async def test_a_deleted_notification_cancels_rather_than_fails(self, wired):
        from app.notify.base import Notification

        queue, notifier, sent = wired
        record = await notifier.send(Notification(recipient="ada@example.com", title="Hello"))
        await notifier.provider.delete(record.pk, CTX)

        notifier.queue_deliveries = False
        await queue.run_once(1)
        job = (await queue.provider.list(ListQuery(), CTX)).items[0]
        assert job["status"] == "done"
        assert "no longer exists" in (job["last_error"] or "")

    @pytest.mark.asyncio
    async def test_no_queue_means_the_notification_is_still_delivered(self, wired):
        """Losing durability must not mean losing the notification."""
        from app.notify.base import Notification

        queue, notifier, sent = wired
        queue.provider = None
        await notifier.send(Notification(recipient="ada@example.com", title="Hello"))
        assert sent == ["Hello"], "the notification was dropped instead of delivered"
