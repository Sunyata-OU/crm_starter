"""Durable background work.

``from app.jobs import queue`` and call ``queue.enqueue("thing", {...})``.
The handlers that run them are registered with ``@register("thing")``; see
:mod:`app.jobs.base` for the design and :mod:`app.jobs.handlers` for the
shipped ones.
"""

from __future__ import annotations

from app.jobs.base import (
    Cancelled,
    Job,
    JobStatus,
    Retry,
    handler_for,
    register,
    registered_kinds,
)
from app.jobs.queue import JobQueue, queue

__all__ = [
    "Cancelled",
    "Job",
    "JobQueue",
    "JobStatus",
    "Retry",
    "handler_for",
    "queue",
    "register",
    "registered_kinds",
]
