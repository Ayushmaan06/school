"""Postgres-backed job queue (ADR-012). No Redis, no Celery.

Durable, inspectable in SQL, one fewer service. The claim statement is the one
specified in docs/DATA-MODEL.md and is used verbatim - `FOR UPDATE SKIP LOCKED`
is what makes concurrent workers safe, so do not rewrite it into an ORM query.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

# Retry backoff by attempt number. After the last one the job goes to 'dead'.
BACKOFF_SECONDS = (2, 8, 30)

# A worker that crashes leaves its rows in 'running' forever. Anything locked
# longer than this is assumed orphaned and returned to the queue.
STALE_LOCK = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


def enqueue(
    session: Session,
    kind: str,
    payload: dict[str, Any],
    dedupe_key: str | None = None,
    not_before: datetime | None = None,
) -> int | None:
    """Insert a job. Returns its id, or None if `dedupe_key` already existed.

    Silently no-opping on a duplicate is the point: enqueuing the same fetch
    twice from two code paths must not produce two fetches.
    """
    row = session.execute(
        text(
            "INSERT INTO jobs (kind, payload, dedupe_key, not_before) "
            "VALUES (:kind, cast(:payload as jsonb), :dedupe_key, coalesce(:not_before, now())) "
            "ON CONFLICT (dedupe_key) DO NOTHING "
            "RETURNING id"
        ),
        {
            "kind": kind,
            "payload": _json(payload),
            "dedupe_key": dedupe_key,
            "not_before": not_before,
        },
    ).first()
    return row[0] if row else None


def claim(session: Session, worker_id: str) -> Job | None:
    """Atomically take one due job. Two concurrent claimers never get the same row."""
    row = session.execute(
        text(
            "UPDATE jobs SET state='running', locked_by=:worker, locked_at=now(), "
            "attempts=attempts+1 "
            "WHERE id = ("
            "  SELECT id FROM jobs"
            "  WHERE state='pending' AND not_before <= now()"
            "  ORDER BY not_before"
            "  FOR UPDATE SKIP LOCKED"
            "  LIMIT 1"
            ") "
            "RETURNING id, kind, payload, attempts, max_attempts"
        ),
        {"worker": worker_id},
    ).first()
    if row is None:
        return None
    return Job(
        id=row[0], kind=row[1], payload=row[2], attempts=row[3], max_attempts=row[4]
    )


def complete(session: Session, job_id: int) -> None:
    session.execute(
        text(
            "UPDATE jobs SET state='done', finished_at=now(), locked_by=NULL, "
            "locked_at=NULL, last_error=NULL WHERE id=:id"
        ),
        {"id": job_id},
    )


def fail(session: Session, job_id: int, error: str) -> None:
    """Re-queue with backoff until attempts are exhausted, then mark dead.

    `attempts` was already incremented by claim(), so it counts tries made.
    """
    row = session.execute(
        text("SELECT attempts, max_attempts FROM jobs WHERE id=:id"), {"id": job_id}
    ).first()
    if row is None:
        return
    attempts, max_attempts = row
    if attempts >= max_attempts:
        session.execute(
            text(
                "UPDATE jobs SET state='dead', finished_at=now(), last_error=:err, "
                "locked_by=NULL, locked_at=NULL WHERE id=:id"
            ),
            {"id": job_id, "err": error[:4000]},
        )
        return
    delay = BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS)) - 1]
    session.execute(
        text(
            "UPDATE jobs SET state='pending', last_error=:err, locked_by=NULL, "
            "locked_at=NULL, not_before = now() + make_interval(secs => :delay) WHERE id=:id"
        ),
        {"id": job_id, "err": error[:4000], "delay": delay},
    )


def sweep_stale_locks(session: Session, older_than: timedelta = STALE_LOCK) -> int:
    """Return jobs orphaned by a crashed worker to 'pending'. Run at startup."""
    result = session.execute(
        text(
            "UPDATE jobs SET state='pending', locked_by=NULL, locked_at=NULL "
            "WHERE state='running' AND locked_at < now() - make_interval(secs => :secs)"
        ),
        {"secs": int(older_than.total_seconds())},
    )
    return result.rowcount or 0


def _json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload)
