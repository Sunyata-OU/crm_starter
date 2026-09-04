"""The queue: enqueueing, claiming, running, and the worker loop.

Backed by a resource like everything else, so the queue is a table you can look
at, filter and sort in the application itself -- and so it is not tied to
PostgreSQL. Any provider that supports a conditional write can hold the queue,
which includes the in-memory one, which is why the tests below are not
integration tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from app.core.clock import utcnow
from app.core.errors import UnsupportedOperation
from app.core.query import Condition, ListQuery, Op, Sort, SortDir, and_, or_
from app.core.results import Ctx, Record
from app.jobs.base import (
    DEFAULT_MAX_ATTEMPTS,
    Cancelled,
    Job,
    JobStatus,
    Retry,
    handler_for,
    registered_kinds,
)

log = logging.getLogger("crm.jobs")

#: How long a claim is trusted before the job is assumed abandoned. Longer than
#: any job should take and shorter than anyone will wait to notice: a worker
#: killed mid-job leaves a row marked running, and without this it stays that
#: way for ever.
DEFAULT_LEASE_SECONDS = 900.0

#: A job that runs longer than this is cancelled and retried. A handler with no
#: timeout of its own would otherwise hold its worker slot indefinitely.
DEFAULT_JOB_TIMEOUT = 300.0

#: Idle wait between polls. The queue is a table, so this is a query; a second
#: is short enough to feel immediate and cheap enough to leave running.
DEFAULT_POLL_SECONDS = 1.0


def worker_name() -> str:
    """Who is holding a claim. Host and pid, so a stuck job names a process."""
    return f"{socket.gethostname()}:{os.getpid()}"


class JobQueue:
    """Durable background work.

    Process-wide, like the notifier and the audit sink, because the places that
    enqueue -- an action, a route, a provider hook -- have no registry to hand.
    """

    def __init__(self) -> None:
        self.provider: Any = None
        self.name = worker_name()
        self.lease = DEFAULT_LEASE_SECONDS
        self.job_timeout = DEFAULT_JOB_TIMEOUT
        self._stopping = asyncio.Event()
        self._warned_about_claiming = False
        self._warned_kinds: set[str] = set()

    # -- wiring -------------------------------------------------------------

    def bind(self, provider: Any) -> None:
        self.provider = provider

    @property
    def configured(self) -> bool:
        return self.provider is not None

    # -- enqueueing ---------------------------------------------------------

    async def enqueue(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        delay: float = 0.0,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        key: str = "",
        priority: int = 0,
        ctx: Ctx | None = None,
    ) -> Record | None:
        """Accept a piece of work, durably.

        Returns the stored row, or ``None`` when there is no queue configured
        -- which the caller must treat as "this will not happen", not as
        "probably fine". :meth:`enqueue_or_run` is the version that decides.

        ``key`` makes the call idempotent: a queued job with the same key is
        returned rather than a second one being created. Only against queued
        work, deliberately -- a job that has already run and is enqueued again
        with the same key is a *new* request to do the thing again, not a
        duplicate of the finished one.
        """
        if self.provider is None:
            log.warning("no job queue configured; %r was not enqueued", kind)
            return None

        system = ctx or Ctx.system()
        if key:
            existing = await self._find_queued(key, system)
            if existing is not None:
                return existing

        job = Job(
            kind=kind,
            payload=payload or {},
            run_at=utcnow() + timedelta(seconds=delay) if delay else utcnow(),
            max_attempts=max_attempts,
            key=key,
            priority=priority,
        )
        if handler_for(kind) is None and kind not in self._warned_kinds:
            # Not a refusal: a deployment may enqueue work for a handler that
            # ships in the next release, and the row waiting is better than the
            # request being dropped. Said once, so the log is not a wall.
            self._warned_kinds.add(kind)
            log.warning(
                "job kind %r has no registered handler; it will wait until one "
                "is deployed (registered kinds: %s)",
                kind, ", ".join(registered_kinds()) or "none",
            )
        try:
            result = await self.provider.create(job.as_row(), system)
        except Exception:
            log.exception("could not enqueue a %r job", kind)
            return None
        return result.record

    async def enqueue_or_run(self, kind: str, payload: dict[str, Any] | None = None, **kw: Any) -> bool:
        """Queue the work if there is a queue, and otherwise do it now.

        For callers that want durability where it is available without becoming
        unusable where it is not -- a test, a one-process deployment, a CLI
        command. Returns True when the work was queued, False when it was run
        inline, so a caller that needs to know can say so.
        """
        if self.provider is not None:
            record = await self.enqueue(kind, payload, **kw)
            if record is not None:
                return True
        handler = handler_for(kind)
        if handler is None:
            log.error("no queue and no handler for job kind %r; dropping it", kind)
            return False
        await handler(Job(kind=kind, payload=payload or {}))
        return False

    async def _find_queued(self, key: str, ctx: Ctx) -> Record | None:
        page = await self.provider.list(
            ListQuery(
                filter=and_(
                    Condition("key", Op.EQ, key),
                    Condition("status", Op.IN, [JobStatus.QUEUED, JobStatus.RUNNING]),
                ),
                page_size=1,
                with_total=False,
            ),
            ctx,
        )
        return page.items[0] if page.items else None

    # -- claiming -----------------------------------------------------------

    async def claim(self, limit: int = 1, ctx: Ctx | None = None) -> list[Job]:
        """Take ownership of up to ``limit`` due jobs.

        Two workers asking at the same moment see the same rows; the
        conditional write is what makes only one of them get each. A worker
        that loses the race simply moves on, which is why this returns however
        many it won rather than however many it asked for.
        """
        if self.provider is None:
            return []
        system = ctx or Ctx.system()

        # Ask for more than needed: some of these will be claimed by another
        # worker between the read and the write, and a second round trip to
        # discover that is worse than reading a few extra rows.
        page = await self.provider.list(
            ListQuery(
                filter=and_(
                    Condition("status", Op.EQ, str(JobStatus.QUEUED)),
                    Condition("run_at", Op.LTE, utcnow()),
                ),
                sort=(Sort("priority", SortDir.DESC), Sort("run_at")),
                page_size=max(limit * 2, limit + 4),
                with_total=False,
            ),
            system,
        )

        won: list[Job] = []
        for record in page.items:
            if len(won) >= limit:
                break
            job = Job.from_row(record)
            if handler_for(job.kind) is None:
                # Left queued rather than claimed and failed: the handler may
                # be one deploy away, and burning its attempts in the meantime
                # would park work that was going to be fine.
                if job.kind not in self._warned_kinds:
                    self._warned_kinds.add(job.kind)
                    log.warning("job %s is a %r, which nothing handles; leaving it queued",
                                job.id, job.kind)
                continue
            if await self._take(job, system):
                won.append(job)
        return won

    async def _take(self, job: Job, ctx: Ctx) -> bool:
        """Stamp this worker's name on one job. False if somebody else did."""
        assert self.provider is not None
        claim = {
            "status": str(JobStatus.RUNNING),
            "claimed_by": self.name,
            "claimed_at": utcnow(),
            # Counted at the claim, not at the failure: a worker killed
            # mid-job has still used an attempt, and not counting it is how a
            # job that reliably kills its worker runs for ever.
            "attempts": job.attempts + 1,
        }
        try:
            result = await self.provider.update_if(
                job.id, claim, {"status": str(JobStatus.QUEUED)}, ctx
            )
        except UnsupportedOperation:
            if not self._warned_about_claiming:
                self._warned_about_claiming = True
                log.warning(
                    "the jobs provider cannot claim rows atomically; run exactly "
                    "one worker, or every job will run once per worker"
                )
            await self.provider.update(job.id, claim, ctx)
            job.attempts += 1
            return True
        if result is None:
            return False
        job.attempts += 1
        job.claimed_by = self.name
        job.status = JobStatus.RUNNING
        return True

    async def release_stale(self, ctx: Ctx | None = None) -> int:
        """Return abandoned jobs to the queue.

        A worker killed between claiming and finishing leaves a row marked
        running that nothing will ever finish. The lease is what distinguishes
        that from a job still legitimately in progress; a job over its lease is
        assumed dead and made eligible again, having already spent an attempt.
        """
        if self.provider is None:
            return 0
        system = ctx or Ctx.system()
        cutoff = utcnow() - timedelta(seconds=self.lease)
        page = await self.provider.list(
            ListQuery(
                filter=and_(
                    Condition("status", Op.EQ, str(JobStatus.RUNNING)),
                    or_(
                        Condition("claimed_at", Op.LT, cutoff),
                        Condition("claimed_at", Op.IS_NULL),
                    ),
                ),
                page_size=200,
                with_total=False,
            ),
            system,
        )
        released = 0
        for record in page.items:
            job = Job.from_row(record)
            status, changes = self._outcome_of_abandonment(job)
            try:
                result = await self.provider.update_if(
                    job.id, changes, {"status": str(JobStatus.RUNNING)}, system
                )
            except UnsupportedOperation:
                await self.provider.update(job.id, changes, system)
                result = record
            if result is not None:
                released += 1
                log.warning(
                    "job %s (%s) was abandoned by %s; %s",
                    job.id, job.kind, job.claimed_by or "an unknown worker", status,
                )
        return released

    def _outcome_of_abandonment(self, job: Job) -> tuple[str, dict[str, Any]]:
        """What becomes of a job whose worker vanished.

        Retried if it has attempts left, and parked if it does not -- a job
        that kills its worker every time is exactly what the attempt limit is
        for, and letting it loop for ever is how one bad payload takes a queue
        down.
        """
        detail = f"worker {job.claimed_by or 'unknown'} stopped without finishing"
        if job.attempts_left <= 0:
            return "parked as failed", {
                "status": str(JobStatus.FAILED),
                "finished_at": utcnow(),
                "last_error": detail,
            }
        return "requeued", {
            "status": str(JobStatus.QUEUED),
            "claimed_by": None,
            "claimed_at": None,
            "run_at": job.next_run(),
            "last_error": detail,
        }

    # -- running ------------------------------------------------------------

    async def run(self, job: Job, ctx: Ctx | None = None) -> bool:
        """Run one claimed job and record what happened. True if it succeeded.

        Every outcome is written down. A job that finishes and is not recorded
        is a job that runs again, which is the price of at-least-once -- but a
        job that is *never* recorded is a queue slowly filling with rows nobody
        can explain.
        """
        system = ctx or Ctx.system()
        handler = handler_for(job.kind)
        if handler is None:
            await self._finish(job, JobStatus.QUEUED, "no handler registered", system)
            return False

        try:
            async with asyncio.timeout(self.job_timeout):
                await handler(job)
        except Cancelled as exc:
            # Not a failure: the handler decided the work should not happen.
            await self._finish(job, JobStatus.DONE, f"cancelled: {exc}", system)
            return True
        except Retry as exc:
            await self._reschedule(job, exc.reason or "retry requested", system, delay=exc.delay)
            return False
        except TimeoutError:
            await self._reschedule(
                job, f"timed out after {self.job_timeout:.0f}s", system
            )
            return False
        except asyncio.CancelledError:
            # The worker is shutting down. Put the job back rather than burning
            # an attempt on an interruption that was nobody's fault, and
            # re-raise so the shutdown proceeds.
            await self._reschedule(job, "worker shut down mid-job", system, delay=0)
            raise
        except Exception as exc:
            # Anything a handler did not ask to be retried. Repeating it would
            # produce the same exception and delay the report of a real bug.
            log.exception("job %s (%s) failed", job.id, job.kind)
            await self._finish(job, JobStatus.FAILED, f"{type(exc).__name__}: {exc}", system)
            return False

        await self._finish(job, JobStatus.DONE, "", system)
        return True

    async def _reschedule(
        self, job: Job, reason: str, ctx: Ctx, *, delay: float | None = None
    ) -> None:
        if job.attempts_left <= 0:
            log.warning(
                "job %s (%s) failed %d times and will not be retried: %s",
                job.id, job.kind, job.attempts, reason,
            )
            await self._finish(job, JobStatus.FAILED, reason, ctx)
            return
        run_at = job.next_run(delay=delay)
        log.info(
            "job %s (%s) will be retried at %s (%d attempt(s) left): %s",
            job.id, job.kind, run_at.isoformat(timespec="seconds"), job.attempts_left, reason,
        )
        await self._write(
            job,
            {
                "status": str(JobStatus.QUEUED),
                "claimed_by": None,
                "claimed_at": None,
                "run_at": run_at,
                "last_error": reason,
            },
            ctx,
        )

    async def _finish(self, job: Job, status: JobStatus, detail: str, ctx: Ctx) -> None:
        changes: dict[str, Any] = {"status": str(status), "last_error": detail or None}
        if status in (JobStatus.DONE, JobStatus.FAILED):
            changes["finished_at"] = utcnow()
        if status is JobStatus.QUEUED:
            changes["claimed_by"] = None
            changes["claimed_at"] = None
        await self._write(job, changes, ctx)

    async def _write(self, job: Job, changes: dict[str, Any], ctx: Ctx) -> None:
        if self.provider is None or job.id is None:
            return
        try:
            await self.provider.update(job.id, changes, ctx)
        except Exception:
            # The job ran; only the bookkeeping failed. Logged loudly because
            # the row is now stuck running and the lease is what will free it.
            log.exception("could not record the outcome of job %s", job.id)

    # -- the loop -----------------------------------------------------------

    async def run_once(self, limit: int = 1, ctx: Ctx | None = None) -> int:
        """Claim and run up to ``limit`` jobs. Returns how many were run."""
        jobs = await self.claim(limit, ctx)
        if not jobs:
            return 0
        await asyncio.gather(*(self.run(job, ctx) for job in jobs))
        return len(jobs)

    async def work(
        self,
        *,
        concurrency: int = 4,
        poll: float = DEFAULT_POLL_SECONDS,
        ctx: Ctx | None = None,
        max_jobs: int = 0,
    ) -> int:
        """Run jobs until asked to stop.

        Polling rather than listening: the queue is a table, so this is one
        indexed query per idle second and works over every backend rather than
        only the one with a notification mechanism.

        ``max_jobs`` stops after that many, for tests and for a worker a
        supervisor is expected to restart periodically.
        """
        self._stopping.clear()
        await self.release_stale(ctx)
        done = 0
        while not self._stopping.is_set():
            budget = concurrency if not max_jobs else min(concurrency, max_jobs - done)
            ran = await self.run_once(budget, ctx) if budget > 0 else 0
            done += ran
            if max_jobs and done >= max_jobs:
                break
            if ran == 0:
                # Nothing due. Wait, but wake immediately if asked to stop, so
                # a shutdown is not held up by the poll interval.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=poll)
        return done

    def stop(self) -> None:
        """Ask the loop to finish the jobs in hand and return."""
        self._stopping.set()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    # -- diagnostics --------------------------------------------------------

    async def counts(self, ctx: Ctx | None = None) -> dict[str, int]:
        """How many jobs are in each state, for ``crm jobs`` and the system page."""
        if self.provider is None:
            return {}
        from app.core.query import Agg, AggSpec, Measure

        rows = await self.provider.aggregate(
            AggSpec(group_by=("status",), measures=(Measure(Agg.COUNT, alias="count"),)),
            ctx or Ctx.system(),
        )
        return {str(row.get("status")): int(row.get("count") or 0) for row in rows}

    async def failed(self, limit: int = 20, ctx: Ctx | None = None) -> Sequence[Record]:
        if self.provider is None:
            return []
        page = await self.provider.list(
            ListQuery(
                filter=Condition("status", Op.EQ, str(JobStatus.FAILED)),
                sort=(Sort("finished_at", SortDir.DESC),),
                page_size=limit,
                with_total=False,
            ),
            ctx or Ctx.system(),
        )
        return page.items

    async def retry(self, pk: Any, ctx: Ctx | None = None) -> bool:
        """Put a failed job back on the queue, with its attempts reset.

        The manual counterpart to the automatic retry: something was wrong,
        somebody fixed it, and the job should run again. Resetting the count is
        the point -- otherwise a job that failed five times is unretryable
        exactly when you have fixed the reason.
        """
        if self.provider is None:
            return False
        await self.provider.update(
            pk,
            {
                "status": str(JobStatus.QUEUED),
                "attempts": 0,
                "run_at": utcnow(),
                "claimed_by": None,
                "claimed_at": None,
                "finished_at": None,
            },
            ctx or Ctx.system(),
        )
        return True


#: The process-wide queue.
queue = JobQueue()
