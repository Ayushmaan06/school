"""Single worker loop.

Stage modules register their handlers in HANDLERS. A handler exception must never
kill the loop - it fails the job, which retries with backoff and eventually dies,
and the worker carries on.
"""

import asyncio
import logging
import signal
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from school_intel.db import session_scope
from school_intel.jobs import queue

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]

# Populated by each stage module at import time, e.g.
#   HANDLERS["fetch"] = fetch.handle
HANDLERS: dict[str, Handler] = {}

IDLE_SLEEP = 1.0


def load_handlers() -> dict[str, Handler]:
    """Import the handler module for its registrations, and return them.

    Registration happens as an import side effect, so a worker started without
    importing `school_intel.jobs.handlers` would claim jobs, find no handler,
    and fail every one of them - looking exactly like a broken queue. Calling
    this makes the dependency explicit instead of relying on import order.

    Imported here rather than at module scope because handlers imports worker.
    """
    from school_intel.jobs import handlers  # noqa: F401  (registers by importing)

    return HANDLERS


class _Stopper:
    """SIGTERM/SIGINT set this; in-flight jobs finish, then the loop exits."""

    def __init__(self) -> None:
        self.stop = False

    def request(self, *_: object) -> None:
        log.info("shutdown requested; finishing in-flight jobs")
        self.stop = True


async def _run_one(worker_id: str, stopper: _Stopper) -> bool:
    """Claim and run at most one job. Returns True if it did work."""
    with session_scope() as session:
        job = queue.claim(session, worker_id)
    if job is None:
        return False

    handler = HANDLERS.get(job.kind)
    if handler is None:
        with session_scope() as session:
            queue.fail(session, job.id, f"no handler registered for kind={job.kind!r}")
        return True

    try:
        await handler(job.payload)
    except Exception as exc:
        log.exception("job %s (%s) failed", job.id, job.kind)
        with session_scope() as session:
            queue.fail(session, job.id, f"{type(exc).__name__}: {exc}")
    else:
        with session_scope() as session:
            queue.complete(session, job.id)
    return True


async def run(concurrency: int = 8) -> None:
    load_handlers()
    worker_id = f"{uuid.uuid4().hex[:8]}"
    stopper = _Stopper()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, stopper.request)
        except ValueError:  # not the main thread; the caller handles shutdown
            pass

    with session_scope() as session:
        swept = queue.sweep_stale_locks(session)
    if swept:
        log.info("returned %d stale job(s) to pending", swept)

    log.info(
        "worker %s started, concurrency=%d, handlers=%s",
        worker_id,
        concurrency,
        sorted(HANDLERS),
    )
    slots = asyncio.Semaphore(concurrency)

    async def _slot() -> bool:
        async with slots:
            return await _run_one(worker_id, stopper)

    # ponytail: batched rounds - launch `concurrency` claims, wait for all, repeat.
    # Ceiling: one slow job holds up its whole round. Switch to a persistent task
    # pool if p99 job latency starts tracking the slowest job in each batch.
    while not stopper.stop:
        did_work = await asyncio.gather(*(_slot() for _ in range(concurrency)))
        if not any(did_work):
            await asyncio.sleep(IDLE_SLEEP)
    log.info("worker %s stopped", worker_id)
