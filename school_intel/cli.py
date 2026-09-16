"""Typer entrypoint. Stage commands; each delegates into its stage module.

Stages are the ones in docs/ARCHITECTURE.md. Commands are stubs until their
milestone lands ,  a stub raises NotImplementedError rather than silently
succeeding, so `--help` is honest about what exists.
"""

from datetime import UTC
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import text

app = typer.Typer(
    name="school-intel",
    help="Rank Indian institutions as recruitment channels for Tensor School.",
    no_args_is_help=True,
)


@app.command(name="import")
def import_(
    source: Annotated[str, typer.Argument(help="Source id, e.g. cbse_saras")],
    state: Annotated[
        list[str] | None,
        typer.Option(
            "--state",
            help="State code(s). Defaults to config target_states (KA, TN, MH).",
        ),
    ] = None,
) -> None:
    """STAGE 1 - run a source importer.

    Import is national by default (ADR-017); --state narrows it for development.
    Enrichment scope is a separate decision made in M2-1.
    """
    import asyncio
    import logging

    from school_intel.config import get_settings
    from school_intel.db import session_scope
    from school_intel.fetch.client import Fetcher
    from school_intel.sources import cbse_saras, cisce

    if source not in {cbse_saras.SOURCE_ID, cisce.SOURCE_ID}:
        raise typer.BadParameter(f"unknown or unimplemented source: {source}")
    if source == cisce.SOURCE_ID and state:
        # The locator's `state` filter takes a free-text state NAME, not the
        # numeric codes --state carries for SARAS. Refuse rather than silently
        # fetch the whole country under a flag the caller thinks narrowed it.
        raise typer.BadParameter(
            "--state is not supported for cisce; it imports nationally"
        )

    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    async def _run() -> None:
        async with Fetcher(settings.user_agent, settings.raw_store_path) as fetcher:
            with session_scope() as session:
                importer: object
                if source == cisce.SOURCE_ID:
                    importer = cisce.CisceImporter(fetcher, session)
                else:
                    importer = cbse_saras.SarasImporter(
                        fetcher, session, states=list(state) if state else None
                    )
                result = await importer.run()
        typer.echo(
            f"rows={result.rows_seen} enqueued={result.jobs_enqueued} "
            f"snapshot={result.snapshot_id}"
        )

    asyncio.run(_run())


@app.command()
def fetch() -> None:
    """STAGE 2 - drain queued fetch jobs."""
    raise NotImplementedError("M1-2")


@app.command(name="enrich")
def enrich(
    limit: Annotated[
        int | None,
        typer.Option(
            min=1, help="Institutions to process. Omit for all; 0 is rejected."
        ),
    ] = None,
    state: Annotated[list[str] | None, typer.Option("--state")] = None,
    per_state: Annotated[
        int | None,
        typer.Option(
            min=1,
            help="Enrich at most N schools PER STATE. Use this for a national"
            " pass - a plain --limit would spend the whole budget on one state.",
        ),
    ] = None,
    redo_after_days: Annotated[
        int | None,
        typer.Option(
            min=0,
            help="Re-visit schools enriched more than N days ago. Defaults to"
            " 180, which is how often fee structures change. Use 0 to force a"
            " full re-visit.",
        ),
    ] = None,
    board: Annotated[
        str | None,
        typer.Option(
            help="Enrich one registry only: 'cbse' or 'cisce'. Omit for both."
            " Keeps a campaign's yield readable - a mixed run averages a fresh"
            " registry against a half-worked one.",
        ),
    ] = None,
) -> None:
    """STAGE 2+3 - MPD discovery and fee extraction (M2-1, M2-2, M2-2b).

    Reports per-hop coverage, because "homepage unreachable", "no MPD page",
    "no fee link" and "unreadable fee document" fail for different reasons and
    need different fixes.
    """
    import asyncio
    import json
    import logging
    from datetime import datetime

    from school_intel.config import get_settings
    from school_intel.db import session_scope
    from school_intel.extract.mpd import run as mpd_run
    from school_intel.fetch.client import Fetcher

    if board is not None and board not in mpd_run.BOARD_ID_COLUMNS:
        raise typer.BadParameter(
            f"--board must be one of {sorted(mpd_run.BOARD_ID_COLUMNS)}; got {board!r}"
        )

    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    async def _go() -> None:
        async with Fetcher(settings.user_agent, settings.raw_store_path) as fetcher:
            with session_scope() as session:
                report = await mpd_run.discover_and_extract(
                    fetcher,
                    session,
                    limit=limit,
                    states=list(state) if state else None,
                    observed_at=datetime.now(UTC),
                    per_state=per_state,
                    redo_after_days=redo_after_days,
                    board=board,
                )
                session.execute(
                    text(
                        "INSERT INTO pipeline_runs (stage, finished_at, rows_in,"
                        " rows_out, ok, notes) VALUES ('enrich', now(), :i, :o, true,"
                        " cast(:n as jsonb))"
                    ),
                    {
                        "i": report.considered,
                        "o": report.fee_found,
                        "n": json.dumps(report.as_dict()),
                    },
                )
        typer.echo(json.dumps(report.as_dict(), indent=2))

    asyncio.run(_go())


@app.command()
def extract(
    reextract: Annotated[
        bool,
        typer.Option(
            "--reextract",
            help="Re-run extractors over stored raw_documents. Zero fetches."
            " INCREMENTAL: documents already read by this version of the"
            " extractors are skipped.",
        ),
    ] = False,
    full: Annotated[
        bool,
        typer.Option(
            "--full",
            help="With --reextract, ignore the extraction_runs markers and"
            " re-read every stored document. Hours of OCR - only needed when a"
            " parser changed WITHOUT its version string changing.",
        ),
    ] = False,
) -> None:
    """STAGE 3 - raw documents -> observations.

    Makes NO HTTP requests. --reextract is the same code path: re-running an
    improved parser over stored bytes is the payoff for ADR-009.

    --reextract is incremental. It is a PARSER-CHANGE operation, not something
    to run after every enrichment pass: the enrich stage already extracts what
    it fetches. Bumping an extractor's version string is what makes its stored
    documents eligible again.
    """
    import logging

    from school_intel.config import get_settings
    from school_intel.db import session_scope
    from school_intel.extract import cisce, saras

    logging.basicConfig(level=get_settings().log_level)
    from datetime import datetime

    from school_intel.extract.mpd import run as mpd_run

    with session_scope() as session:
        result = saras.run(session)
        cisce_result = cisce.run(session)
        mpd = mpd_run.reextract(session, observed_at=datetime.now(UTC), full=full)
    typer.echo(
        f"saras: documents={result.documents} entities={result.entities} "
        f"observations={result.observations}"
    )
    typer.echo(
        f"cisce: documents={cisce_result.documents} entities={cisce_result.entities} "
        f"observations={cisce_result.observations}"
    )
    typer.echo(
        f"mpd:   institutions={mpd.considered} fees={mpd.fee_found} "
        f"(ocr {mpd.fee_from_ocr}) contacts={mpd.contacts_found} "
        f"unreadable={mpd.unreadable_fee}"
    )
    if reextract:
        typer.echo("(--reextract made zero fetches; ADR-009)")


@app.command()
def resolve(
    rebuild: Annotated[
        bool,
        typer.Option("--rebuild", help="Full recompute. This is the normal mode."),
    ] = True,
) -> None:
    """STAGE 5 - observations -> canonical institutions.

    Truncates ONLY the canonical layer and replays it. Never touches
    observations, fetches or raw_documents (hard rule 4).
    """
    import logging
    from datetime import datetime

    from school_intel.config import get_settings
    from school_intel.db import session_scope
    from school_intel.resolve import build

    logging.basicConfig(level=get_settings().log_level)
    # run_at is generated ONCE here and passed in, never called inside the
    # resolver - determinism depends on it.
    run_at = datetime.now(UTC)
    with session_scope() as session:
        result = build.rebuild(session, run_at)
    typer.echo(
        f"institutions={result.institutions} linked={result.observations_linked} "
        f"conflicts={result.conflicts} flags={result.flags}"
    )


@app.command()
def score(
    campaign: Annotated[str, typer.Option(help="Scoring campaign profile")] = "default",
) -> None:
    """STAGE 7 - Fit and Confidence, two numbers, never blended (ADR-010)."""
    import logging
    from datetime import datetime

    from school_intel.config import get_settings
    from school_intel.db import session_scope
    from school_intel.resolve import groups
    from school_intel.score import run as score_run

    logging.basicConfig(level=get_settings().log_level)
    now = datetime.now(UTC)
    with session_scope() as session:
        # M4-1b runs BETWEEN resolve and score: group leverage reads
        # qualifying_campus_count, which must already exist.
        rollup = groups.build_groups(session)
        result = score_run.run(session, now, campaign=campaign)
    typer.echo(
        f"groups={rollup.groups} qualifying_campuses={rollup.qualifying_campuses}"
    )
    typer.echo(
        f"scored={result.scored} gated_out={result.gated_out} "
        f"insufficient_data={result.insufficient_data}"
    )
    typer.echo(f"flags={result.flags}")


@app.command()
def signals() -> None:
    """STAGE 8 - diff registry snapshots."""
    raise NotImplementedError("M4-3")


@app.command(name="eval")
def eval_(
    extractor: Annotated[str, typer.Option(help="Label for this run's report")] = "all",
    write: Annotated[
        bool, typer.Option(help="Write the report to tests/eval/results/")
    ] = True,
) -> None:
    """ADR-015 - per-field precision and recall against the gold set.

    A prompt, parser or model change that does not report its eval delta is not
    reviewable, so the delta against the previous run is always printed.
    """
    from school_intel import eval as harness
    from school_intel.db import session_scope

    with session_scope() as session:
        report = harness.run(session, extractor=extractor)
    previous = harness.previous_report(report)
    typer.echo(harness.format_table(report, previous))
    if write:
        path = harness.write_report(report)
        typer.echo("")
        typer.echo(f"report written to {path}")


@app.command(name="load-gold")
def load_gold(
    path: Annotated[Path, typer.Argument(help="Hand-verified gold CSV")],
) -> None:
    """Load hand-verified ground truth. NEVER machine-written (ADR-015)."""
    import csv
    import json

    from school_intel.db import session_scope

    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    with session_scope() as session:
        for row in rows:
            session.execute(
                text(
                    "INSERT INTO eval_gold (institution_key, field, expected_json,"
                    " verified_by, verified_at, note)"
                    " VALUES (:k, :f, cast(:v as jsonb), :by, cast(:on as date), :n)"
                    " ON CONFLICT (institution_key, field) DO UPDATE SET"
                    " expected_json = EXCLUDED.expected_json,"
                    " verified_by = EXCLUDED.verified_by,"
                    " verified_at = EXCLUDED.verified_at, note = EXCLUDED.note"
                ),
                {
                    "k": row["institution_key"],
                    "f": row["field"],
                    "v": json.dumps(row["expected"]),
                    "by": row["verified_by"],
                    "on": row["verified_at"],
                    "n": row.get("note"),
                },
            )
    typer.echo(f"loaded {len(rows)} gold rows")


@app.command()
def serve(port: int = 8000, host: str = "127.0.0.1") -> None:
    """STAGE 9 - the read-only API and UI. Reads Postgres only."""
    import uvicorn

    uvicorn.run("school_intel.api.app:app", host=host, port=port, reload=False)


@app.command()
def worker(concurrency: int = 8) -> None:
    """Drain the Postgres job queue."""
    import asyncio
    import logging

    from school_intel.config import get_settings
    from school_intel.jobs import handlers

    logging.basicConfig(level=get_settings().log_level)
    asyncio.run(handlers.run_worker(concurrency=concurrency))


@app.command(name="load-pins")
def load_pins(
    path: Annotated[Path, typer.Argument(help="India Post PIN code CSV (S9)")],
) -> None:
    """Load PIN centroids and tag metro areas. Run once (ADR-004)."""
    from school_intel.db import session_scope
    from school_intel.sources.pin_centroids import apply_geography, load_centroids

    centroids = load_centroids(path)
    typer.echo(f"read {len(centroids)} PIN centroids")
    with session_scope() as session:
        stats = apply_geography(session, centroids)
    typer.echo(
        f"institutions with a pincode={stats['with_pincode']} "
        f"located={stats['located']} metro_tagged={stats['metro_tagged']}"
    )


@app.command(name="mark-enriched")
def mark_enriched() -> None:
    """Record which schools have already been visited, from the fetch log.

    Run this after stopping an enrichment run part-way. It is what makes the
    next run resume instead of starting over. Safe to re-run.
    """
    from school_intel.db import session_scope
    from school_intel.extract.mpd import run as mpd_run

    with session_scope() as session:
        marked = mpd_run.backfill_last_enriched(session)
    typer.echo(f"marked {marked} schools as already enriched")


@app.command()
def seed() -> None:
    """Upsert source_registry from sources/seed.yaml. Idempotent."""
    from school_intel.db import session_scope
    from school_intel.sources import seed_sources

    with session_scope() as session:
        count = seed_sources(session)
    typer.echo(f"seeded {count} sources")


@app.command("guess-emails")
def guess_emails(
    all_schools: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Also guess for schools we have never enriched. Off by default:"
            " an unvisited school may still publish a real address, and a guess"
            " sitting there is a reason nobody looks.",
        ),
    ] = False,
    board: Annotated[
        str | None,
        typer.Option(help="One registry only: 'cbse' or 'cisce'. Omit for both."),
    ] = None,
) -> None:
    """Derive <name>@gmail.com from each school's own domain.

    Writes `email_guessed` ONLY - never `email` - so scores and contactable
    counts stay based on published facts. Safe to re-run; it overwrites.
    """
    from school_intel.db import session_scope
    from school_intel.extract.mpd.run import BOARD_ID_COLUMNS
    from school_intel.guess_email import backfill

    if board is not None and board not in BOARD_ID_COLUMNS:
        raise typer.BadParameter(
            f"--board must be one of {sorted(BOARD_ID_COLUMNS)}; got {board!r}"
        )

    with session_scope() as session:
        report = backfill(session, only_weak=not all_schools, board=board)
    typer.echo(" ".join(f"{k}={v}" for k, v in report.items()))


@app.command("guess-student-counts")
def guess_student_counts(
    board: Annotated[
        str | None,
        typer.Option(help="One registry only: 'cbse' or 'cisce'. Omit for both."),
    ] = None,
) -> None:
    """Fill a random placeholder count for enriched, contactable schools with none.

    Draws a random integer between the DB-wide mean and median of
    `total_enrollment` - not derived from the school itself. Writes
    `student_count_guessed` ONLY - never `total_enrollment` - so score and
    `pcm_12_count` stay based on real or teacher-derived numbers. Safe to
    re-run; it draws a fresh value each time.
    """
    from school_intel.db import session_scope
    from school_intel.extract.mpd.run import BOARD_ID_COLUMNS
    from school_intel.guess_student_count import backfill as guess_counts

    if board is not None and board not in BOARD_ID_COLUMNS:
        raise typer.BadParameter(
            f"--board must be one of {sorted(BOARD_ID_COLUMNS)}; got {board!r}"
        )

    with session_scope() as session:
        report = guess_counts(session, board=board)
    typer.echo(" ".join(f"{k}={v}" for k, v in report.items()))


@app.command()
def schedule() -> None:
    """Enqueue imports that are due per source_registry.refresh_days."""
    raise NotImplementedError("M5-6")


if __name__ == "__main__":
    app()
