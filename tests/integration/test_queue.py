"""M0-3: the queue's four correctness properties.

Needs a live Postgres (`make db`) - SKIP LOCKED is the thing under test, so an
in-memory fake would test nothing.
"""

from datetime import UTC, timedelta

import pytest
from sqlalchemy import text

from school_intel.jobs import queue

pytestmark = pytest.mark.usefixtures("clean_jobs")


@pytest.fixture
def clean_jobs(db_session):
    db_session.execute(text("TRUNCATE jobs RESTART IDENTITY"))
    db_session.commit()
    yield


def test_enqueue_and_claim_roundtrip(db_session):
    job_id = queue.enqueue(db_session, "fetch", {"url": "https://example.test/a"})
    db_session.commit()
    assert job_id is not None

    job = queue.claim(db_session, "w1")
    assert job is not None
    assert job.kind == "fetch"
    assert job.payload == {"url": "https://example.test/a"}
    assert job.attempts == 1


def test_dedupe_key_prevents_duplicates(db_session):
    first = queue.enqueue(db_session, "fetch", {"url": "u"}, dedupe_key="cbse:330801")
    second = queue.enqueue(db_session, "fetch", {"url": "u"}, dedupe_key="cbse:330801")
    db_session.commit()
    assert first is not None
    assert second is None
    count = db_session.execute(text("SELECT count(*) FROM jobs")).scalar()
    assert count == 1


def test_two_claimers_never_get_the_same_job(engine):
    """The property SKIP LOCKED exists for. Two real sessions, one row."""
    from sqlalchemy.orm import Session

    with Session(engine) as setup:
        queue.enqueue(setup, "fetch", {"n": 1})
        setup.commit()

    with Session(engine) as a, Session(engine) as b:
        got_a = queue.claim(a, "worker-a")
        got_b = queue.claim(b, "worker-b")
        a.commit()
        b.commit()

    claimed = [j for j in (got_a, got_b) if j is not None]
    assert len(claimed) == 1, "the same job was handed to two workers"


def test_not_before_hides_a_future_job(db_session):
    from datetime import datetime

    queue.enqueue(
        db_session,
        "fetch",
        {"n": 1},
        not_before=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.commit()
    assert queue.claim(db_session, "w1") is None


def test_failure_retries_with_backoff_then_dies(db_session):
    queue.enqueue(db_session, "fetch", {"n": 1})
    db_session.commit()

    for attempt in range(1, 4):
        job = queue.claim(db_session, "w1")
        assert job is not None, f"job was not re-queued before attempt {attempt}"
        assert job.attempts == attempt
        queue.fail(db_session, job.id, "boom")
        db_session.commit()
        # backoff pushes not_before into the future; clear it so the test does
        # not sleep. The delay itself is asserted separately below.
        db_session.execute(text("UPDATE jobs SET not_before = now()"))
        db_session.commit()

    assert queue.claim(db_session, "w1") is None
    state = db_session.execute(text("SELECT state, last_error FROM jobs")).first()
    assert state[0] == "dead"
    assert "boom" in state[1]


def test_failure_sets_a_future_not_before(db_session):
    queue.enqueue(db_session, "fetch", {"n": 1})
    db_session.commit()
    job = queue.claim(db_session, "w1")
    queue.fail(db_session, job.id, "boom")
    db_session.commit()

    row = db_session.execute(
        text("SELECT state, not_before > now() FROM jobs WHERE id=:id"), {"id": job.id}
    ).first()
    assert row[0] == "pending"
    assert row[1] is True, "backoff did not push not_before into the future"


def test_sweep_returns_orphaned_running_jobs(db_session):
    queue.enqueue(db_session, "fetch", {"n": 1})
    db_session.commit()
    job = queue.claim(db_session, "w1")
    db_session.commit()

    # Simulate a worker that died holding the lock.
    db_session.execute(
        text("UPDATE jobs SET locked_at = now() - interval '31 minutes' WHERE id=:id"),
        {"id": job.id},
    )
    db_session.commit()

    assert queue.claim(db_session, "w2") is None, (
        "a running job should not be claimable"
    )
    assert queue.sweep_stale_locks(db_session) == 1
    db_session.commit()
    assert queue.claim(db_session, "w2") is not None


def test_sweep_leaves_a_live_lock_alone(db_session):
    queue.enqueue(db_session, "fetch", {"n": 1})
    db_session.commit()
    queue.claim(db_session, "w1")
    db_session.commit()
    assert queue.sweep_stale_locks(db_session) == 0
