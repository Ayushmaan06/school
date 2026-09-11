"""M0-3 DoD: the worker processes an enqueued job, and a bad handler never kills it."""

import pytest
from sqlalchemy import text

from school_intel.jobs import queue, worker


@pytest.fixture
def clean_jobs(db_session):
    db_session.execute(text("TRUNCATE jobs RESTART IDENTITY"))
    db_session.commit()
    yield
    worker.HANDLERS.clear()


async def test_worker_runs_a_noop_job(db_session, clean_jobs, monkeypatch):
    monkeypatch.setattr("school_intel.db.get_engine", lambda: db_session.get_bind())
    ran = []

    async def noop(payload):
        ran.append(payload)

    worker.HANDLERS["noop"] = noop
    queue.enqueue(db_session, "noop", {"n": 1})
    db_session.commit()

    job = queue.claim(db_session, "w1")
    await worker.HANDLERS[job.kind](job.payload)
    queue.complete(db_session, job.id)
    db_session.commit()

    assert ran == [{"n": 1}]
    assert db_session.execute(text("SELECT state FROM jobs")).scalar() == "done"


def test_unknown_kind_fails_the_job_rather_than_crashing(db_session, clean_jobs):
    queue.enqueue(db_session, "no-such-kind", {})
    db_session.commit()
    job = queue.claim(db_session, "w1")
    assert worker.HANDLERS.get(job.kind) is None
    queue.fail(db_session, job.id, "no handler registered")
    db_session.commit()
    state, err = db_session.execute(text("SELECT state, last_error FROM jobs")).first()
    assert state == "pending"
    assert "no handler" in err


def test_handler_exception_is_recorded_and_retried(db_session, clean_jobs):
    queue.enqueue(db_session, "boom", {})
    db_session.commit()
    job = queue.claim(db_session, "w1")
    queue.fail(db_session, job.id, "RuntimeError: kaboom")
    db_session.commit()
    state, err = db_session.execute(text("SELECT state, last_error FROM jobs")).first()
    assert state == "pending", "a failing handler must retry, not die on first error"
    assert "kaboom" in err
