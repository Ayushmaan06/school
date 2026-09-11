"""M1-1: the SARAS importer. No live network - respx serves the saved fixtures."""

from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import text

from school_intel.extract.parsers.cbse_saras import parse_list
from school_intel.fetch.client import Fetcher
from school_intel.sources import cbse_saras as saras
from school_intel.sources.base import RowCountMismatchError, StructureDriftError

FIXTURES = Path(__file__).parent.parent / "fixtures" / "cbse_saras"
KA_HTML = (FIXTURES / "list_state_KA.html").read_text(encoding="utf-8")
SEED_HTML = (FIXTURES / "list_ID1_D.html").read_text(encoding="utf-8")


@pytest.fixture
def clean(db_session):
    db_session.execute(
        text("TRUNCATE jobs, registry_snapshots, fetches, raw_documents CASCADE")
    )
    db_session.execute(
        text(
            "INSERT INTO source_registry (id, kind, authority_tier, refresh_days,"
            " rate_limit_rps) VALUES ('cbse_saras','registry',1,30,1000)"
            " ON CONFLICT (id) DO UPDATE SET rate_limit_rps = 1000"
        )
    )
    db_session.commit()
    yield


@pytest.fixture
def mock_saras():
    with respx.mock:
        respx.get("https://saras.cbse.gov.in/robots.txt").mock(
            return_value=httpx.Response(
                404, text="<html>404</html>", headers={"content-type": "text/html"}
            )
        )
        respx.get(saras.LIST_SEED_URL).mock(
            return_value=httpx.Response(200, text=SEED_HTML)
        )
        post = respx.post(saras.LIST_POST_URL).mock(
            return_value=httpx.Response(200, text=KA_HTML)
        )
        yield post


# --- pure helpers --------------------------------------------------------


def test_token_is_extracted_from_the_seed_page():
    token = saras.extract_token(SEED_HTML)
    assert token and len(token) > 40


def test_missing_token_returns_none_rather_than_a_guess():
    assert saras.extract_token("<html>no form here</html>") is None


def test_state_codes_are_unique_and_cover_the_enrichment_priority():
    """An earlier hand-written version of this table gave Tamil Nadu and Odisha
    the same code. These are lifted verbatim from the live form."""
    assert len(saras.STATE_CODES) == 38
    assert len(set(saras.STATE_CODES.values())) == 38
    for state in saras.ENRICHMENT_FIRST:
        assert state in saras.STATE_CODES
    assert saras.STATE_CODES["KARNATAKA"] == "8"
    assert saras.STATE_CODES["TAMILNADU"] != saras.STATE_CODES["ODISHA"]


def test_form_uses_the_server_side_hard_gate_when_asked():
    """SchoolStatusWise is a server-side filter, so the hard gate can be a
    request parameter rather than a post-parse step."""
    everything = saras.build_state_form("tok", "8")
    senior = saras.build_state_form("tok", "8", senior_only=True)
    assert everything["SchoolStatusWise"] == saras.SCHOOL_STATUS_ALL
    assert senior["SchoolStatusWise"] == saras.SCHOOL_STATUS_SENIOR_SECONDARY
    assert senior["MainRadioValue"] == "State_wise"
    assert senior["State"] == "8"
    assert senior["__RequestVerificationToken"] == "tok"


# --- guards --------------------------------------------------------------


def test_row_count_mismatch_fails_loudly():
    """A silent shortfall looks exactly like a state having fewer schools."""
    page = parse_list(KA_HTML)
    page.stated_total = 9999
    with pytest.raises(RowCountMismatchError, match="1847"):
        saras.check_row_count(page, "cbse_saras/KARNATAKA")


def test_row_count_check_passes_on_the_real_page():
    saras.check_row_count(parse_list(KA_HTML), "cbse_saras/KARNATAKA")


KARNATAKA_SCOPE = "KARNATAKA"
NATIONAL_SCOPE = ",".join(sorted(saras.STATE_CODES))


def _seed_snapshot(db_session, row_count: int, scope: str = KARNATAKA_SCOPE) -> None:
    db_session.execute(
        text(
            "INSERT INTO raw_documents (content_hash, storage_path, media_type, bytes)"
            " VALUES ('h1','/tmp/h1','text/html',1) ON CONFLICT DO NOTHING"
        )
    )
    saras.write_snapshot(db_session, "h1", row_count, scope)
    db_session.commit()


def test_drift_guard_fires_below_half_the_previous_snapshot(db_session, clean):
    """Hard rule 10. Without this, one parser regression emits thousands of false
    disaffiliation signals in M4-3."""
    _seed_snapshot(db_session, 1847)
    with pytest.raises(
        StructureDriftError, match="Aborting without writing a snapshot"
    ):
        saras.check_drift(db_session, 900, KARNATAKA_SCOPE)


def test_drift_guard_allows_a_normal_fluctuation(db_session, clean):
    _seed_snapshot(db_session, 1847)
    saras.check_drift(db_session, 1800, KARNATAKA_SCOPE)


def test_drift_guard_is_a_noop_on_the_first_ever_run(db_session, clean):
    saras.check_drift(db_session, 10, KARNATAKA_SCOPE)


def test_a_state_run_is_not_measured_against_a_national_snapshot(db_session, clean):
    """[VERIFIED M5] This actually happened. After the national import wrote a
    33,146-row snapshot, importing one state (2,252 rows) tripped the guard at
    "7%" and aborted - correct arithmetic against the wrong baseline. A one-state
    run is not a 93% collapse of the country.
    """
    _seed_snapshot(db_session, 33146, NATIONAL_SCOPE)
    # Must NOT raise: different scope, so there is no comparable baseline.
    saras.check_drift(db_session, 2252, KARNATAKA_SCOPE)


def test_a_national_run_is_still_guarded_against_a_national_baseline(db_session, clean):
    """Scoping must not weaken the guard for the case it exists to catch."""
    _seed_snapshot(db_session, 33146, NATIONAL_SCOPE)
    with pytest.raises(StructureDriftError):
        saras.check_drift(db_session, 4000, NATIONAL_SCOPE)


# --- end to end ----------------------------------------------------------


async def test_import_enqueues_only_senior_secondary(
    db_session, clean, mock_saras, tmp_path
):
    """[VERIFIED M0-0] ~29% of the list. Filtering BEFORE any detail fetch is the
    single largest cost saving in the pipeline (ADR-007)."""
    async with Fetcher("Test/1.0", tmp_path / "raw") as fetcher:
        importer = saras.SarasImporter(fetcher, db_session, states=["KARNATAKA"])
        result = await importer.run()
    db_session.commit()

    assert result.rows_seen == 1847
    assert result.jobs_enqueued == 539
    assert result.snapshot_id is not None

    kinds = db_session.execute(text("SELECT DISTINCT kind FROM jobs")).scalars().all()
    assert kinds == ["fetch_saras_detail"]
    assert db_session.execute(text("SELECT count(*) FROM jobs")).scalar() == 539


async def test_import_posts_the_verified_form(db_session, clean, mock_saras, tmp_path):
    async with Fetcher("Test/1.0", tmp_path / "raw") as fetcher:
        await saras.SarasImporter(fetcher, db_session, states=["KARNATAKA"]).run()
    db_session.commit()

    assert mock_saras.called
    body = mock_saras.calls[0].request.content.decode()
    assert "MainRadioValue=State_wise" in body
    assert "State=8" in body
    assert "__RequestVerificationToken=" in body


async def test_rerunning_the_import_enqueues_nothing_new(
    db_session, clean, mock_saras, tmp_path
):
    """dedupe_key on the affiliation number: two runs must not double the queue."""
    async with Fetcher("Test/1.0", tmp_path / "raw") as fetcher:
        importer = saras.SarasImporter(fetcher, db_session, states=["KARNATAKA"])
        first = await importer.run()
        db_session.commit()
        second = await importer.run()
        db_session.commit()

    assert first.jobs_enqueued == 539
    assert second.jobs_enqueued == 0
    assert db_session.execute(text("SELECT count(*) FROM jobs")).scalar() == 539


async def test_snapshot_records_the_row_count_for_signal_diffing(
    db_session, clean, mock_saras, tmp_path
):
    async with Fetcher("Test/1.0", tmp_path / "raw") as fetcher:
        await saras.SarasImporter(fetcher, db_session, states=["KARNATAKA"]).run()
    db_session.commit()

    row = db_session.execute(
        text("SELECT source_id, row_count FROM registry_snapshots")
    ).first()
    assert row[0] == "cbse_saras"
    assert row[1] == 1847
