"""Job handlers, registered into worker.HANDLERS at import time.

Fetch jobs only fetch and store. Extraction is a separate stage over stored
documents, which is what keeps `extract --reextract` free of network calls
(ADR-009). A handler that fetched *and* extracted would couple the two and
quietly re-crawl every time a parser changed.
"""

import logging
from typing import Any

from school_intel.config import get_settings
from school_intel.db import session_scope
from school_intel.fetch.client import Fetcher
from school_intel.jobs import worker

log = logging.getLogger(__name__)

_fetcher: Fetcher | None = None


def set_fetcher(fetcher: Fetcher | None) -> None:
    """The worker owns one Fetcher for the process, so connection pools, robots
    cache and per-domain limits are shared across jobs rather than per job."""
    global _fetcher
    _fetcher = fetcher


async def fetch_url(payload: dict[str, Any]) -> None:
    """Fetch one URL and store it. Used by every fetch_* job kind."""
    if _fetcher is None:
        raise RuntimeError("no Fetcher set; the worker must call set_fetcher() first")
    url = payload["url"]
    source_id = payload["source_id"]
    with session_scope() as session:
        result = await _fetcher.fetch(session, url, source_id)
    if result.blocked_reason:
        log.info("declined %s (%s)", url, result.blocked_reason)
    elif result.status != 200:
        # Raise so the queue retries with backoff rather than marking it done.
        raise RuntimeError(f"{url}: HTTP {result.status}")


async def enrich_state(payload: dict[str, Any]) -> None:
    """Enrich N schools in one state. Backs the "Get more details" button.

    The button only ENQUEUES (hard rule 3); this is the code that actually
    fetches, and it runs in the worker where the per-domain rate limits and the
    CAPTCHA circuit breaker apply. A click therefore cannot hang a browser or
    stampede a school's shared hosting.
    """
    from datetime import UTC, datetime

    from school_intel.extract.mpd import run as mpd_run

    if _fetcher is None:
        raise RuntimeError("no Fetcher set; the worker must call set_fetcher() first")

    state = payload["state"]
    count = int(payload.get("count") or 250)
    with session_scope() as session:
        report = await mpd_run.discover_and_extract(
            _fetcher,
            session,
            states=[state],
            limit=count,
            observed_at=datetime.now(UTC),
        )
    log.info(
        "enriched %s: considered=%d fees=%d contacts=%d",
        state,
        report.considered,
        report.fee_found,
        report.contacts_found,
    )


worker.HANDLERS["fetch_saras_detail"] = fetch_url
worker.HANDLERS["enrich_state"] = enrich_state


async def run_worker(concurrency: int = 8) -> None:
    """Entrypoint that owns the Fetcher for the worker's lifetime."""
    settings = get_settings()
    async with Fetcher(settings.user_agent, settings.raw_store_path) as fetcher:
        set_fetcher(fetcher)
        try:
            await worker.run(concurrency=concurrency)
        finally:
            set_fetcher(None)
