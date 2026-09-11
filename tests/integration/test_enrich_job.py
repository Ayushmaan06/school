"""The "Get more details" button: enqueue, then the worker does the fetching.

Hard rule 3 says a user request never triggers a fetch. That is not paperwork -
250 fetches inside a web request would time out the browser, bypass the
per-domain rate limits, and lose all its work on a restart.
"""

import pytest
from sqlalchemy import text

from school_intel.jobs import queue, worker


@pytest.fixture
def clean(db_session):
    db_session.execute(text("TRUNCATE jobs RESTART IDENTITY"))
    db_session.commit()
    yield


def test_the_enrich_handler_is_registered():
    """The button queued jobs correctly for a while with nothing to drain them,
    which looks exactly like the button being broken.

    load_handlers() is called rather than relying on someone having imported
    the handlers module: registration is an import side effect, so a worker
    started without that import would fail every job it claimed.
    """
    registered = worker.load_handlers()
    assert "enrich_state" in registered
    assert "fetch_saras_detail" in registered


def test_a_click_enqueues_rather_than_fetching(db_session, clean):
    job_id = queue.enqueue(
        db_session,
        kind="enrich_state",
        payload={"state": "kerala", "count": 250},
        dedupe_key="enrich:kerala:250",
    )
    db_session.commit()
    assert job_id is not None

    row = db_session.execute(
        text("SELECT kind, state, payload FROM jobs WHERE id = :i"), {"i": job_id}
    ).first()
    assert row[0] == "enrich_state"
    assert row[1] == "pending", "queued, not run inside the request"
    assert row[2] == {"state": "kerala", "count": 250}


def test_clicking_twice_does_not_queue_twice(db_session, clean):
    """An impatient double click must not double-crawl the same 250 schools."""
    first = queue.enqueue(
        db_session,
        "enrich_state",
        {"state": "kerala", "count": 250},
        dedupe_key="enrich:kerala:250",
    )
    second = queue.enqueue(
        db_session,
        "enrich_state",
        {"state": "kerala", "count": 250},
        dedupe_key="enrich:kerala:250",
    )
    db_session.commit()
    assert first is not None
    assert second is None
    assert db_session.execute(text("SELECT count(*) FROM jobs")).scalar() == 1


def test_a_different_state_is_a_different_job(db_session, clean):
    queue.enqueue(
        db_session, "enrich_state", {"state": "kerala"}, dedupe_key="enrich:kerala:250"
    )
    queue.enqueue(
        db_session, "enrich_state", {"state": "goa"}, dedupe_key="enrich:goa:250"
    )
    db_session.commit()
    assert db_session.execute(text("SELECT count(*) FROM jobs")).scalar() == 2


def test_the_worker_can_claim_an_enrich_job(db_session, clean):
    queue.enqueue(
        db_session,
        "enrich_state",
        {"state": "kerala", "count": 10},
        dedupe_key="enrich:kerala:10",
    )
    db_session.commit()

    job = queue.claim(db_session, "worker-1")
    assert job is not None
    assert job.kind == "enrich_state"
    assert worker.load_handlers().get(job.kind) is not None, (
        "claimed but nothing to run it"
    )


async def test_the_handler_refuses_to_run_without_a_fetcher(db_session, clean):
    """It must fail loudly rather than silently doing nothing - a job that
    quietly succeeds without fetching is worse than one that retries."""
    from school_intel.jobs import handlers

    handlers.set_fetcher(None)
    with pytest.raises(RuntimeError, match="no Fetcher set"):
        await handlers.enrich_state({"state": "kerala", "count": 1})
