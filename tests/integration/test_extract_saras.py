"""M1-3: stored documents -> observations. No network anywhere in this file."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from school_intel.extract import saras
from school_intel.extract.observations import (
    Claim,
    MissingEvidenceError,
    UnknownFieldError,
    write_observations,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "cbse_saras"
KA_HTML = (FIXTURES / "list_state_KA.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "detail_330801.html").read_text(encoding="utf-8")
WHEN = datetime(2026, 9, 5, tzinfo=UTC)


@pytest.fixture
def clean(db_session):
    db_session.execute(
        text("TRUNCATE observations, fetches, raw_documents, jobs CASCADE")
    )
    db_session.execute(
        text(
            "INSERT INTO source_registry (id, kind, authority_tier, refresh_days)"
            " VALUES ('cbse_saras','registry',1,30) ON CONFLICT DO NOTHING"
        )
    )
    db_session.execute(
        text(
            "INSERT INTO raw_documents (content_hash, storage_path, media_type, bytes)"
            " VALUES ('h_list','/tmp/l','text/html',1), ('h_detail','/tmp/d','text/html',1)"
            " ON CONFLICT DO NOTHING"
        )
    )
    db_session.execute(
        text(
            "INSERT INTO fetches (id, source_id, url, content_hash, http_status,"
            " robots_allowed, requested_at) VALUES"
            " (1,'cbse_saras','https://saras.cbse.gov.in/SARAS/AffiliatedList/"
            "ListOfSchdirReport','h_list',200,true,:w),"
            " (2,'cbse_saras','https://saras.cbse.gov.in/SARAS/AffiliatedList/"
            "AfflicationDetails/330801','h_detail',200,true,:w)"
        ),
        {"w": WHEN},
    )
    db_session.commit()
    yield


# --- the writer's two invariants ----------------------------------------


def test_unknown_field_is_rejected_loudly(db_session, clean):
    """An unknown field name would be written and then silently ignored by the
    resolver, which is worse than failing."""
    with pytest.raises(UnknownFieldError, match="canonical field vocabulary"):
        write_observations(
            db_session,
            claims=[Claim("fee_per_month", "1000", "evidence")],
            entity_key="cbse:1",
            source_id="cbse_saras",
            extractor="test",
            observed_at=WHEN,
        )


def test_missing_evidence_is_rejected_loudly(db_session, clean):
    """An extraction without evidence is a bug at every tier - it breaks
    /institutions/{id}/why, which must not care which tier produced a value."""
    with pytest.raises(MissingEvidenceError, match="evidence_span"):
        write_observations(
            db_session,
            claims=[Claim("name", "A School", "   ")],
            entity_key="cbse:1",
            source_id="cbse_saras",
            extractor="test",
            observed_at=WHEN,
        )


# --- list extraction -----------------------------------------------------


def test_list_extraction_writes_observations_with_evidence(db_session, clean):
    written, seen = saras.extract_list(
        db_session, KA_HTML, fetch_id=1, content_hash="h_list", observed_at=WHEN
    )
    db_session.commit()
    assert seen == 1847
    assert written > 10000

    missing = db_session.execute(
        text(
            "SELECT count(*) FROM observations"
            " WHERE evidence_span IS NULL OR btrim(evidence_span) = ''"
        )
    ).scalar()
    assert missing == 0, "every observation must carry an evidence span"


def test_list_extraction_captures_the_high_value_fields(db_session, clean):
    saras.extract_list(
        db_session, KA_HTML, fetch_id=1, content_hash="h_list", observed_at=WHEN
    )
    db_session.commit()
    got = dict(
        db_session.execute(
            text(
                "SELECT field, value_text FROM observations"
                " WHERE entity_key = 'cbse:800001'"
            )
        ).all()
    )
    assert got["name"] == "PM SHRI KENDRIYA VIDYALAYA ASC CENTRE"
    assert got["principal_name"] == "DR. NUTAN PUNJ"
    assert got["website"] == "https://ascbangalore.kvs.ac.in"
    assert got["has_class_12"] == "true"
    assert got["state"] == "KARNATAKA"


def test_blank_fields_emit_nothing_rather_than_empty_observations(db_session, clean):
    """Hard rule 6: a missing observation beats a wrong or empty one."""
    saras.extract_list(
        db_session, KA_HTML, fetch_id=1, content_hash="h_list", observed_at=WHEN
    )
    db_session.commit()
    empties = db_session.execute(
        text(
            "SELECT count(*) FROM observations WHERE btrim(coalesce(value_text,'')) = ''"
        )
    ).scalar()
    assert empties == 0

    # 5 of 539 senior-secondary rows have no website; those must be absent, not blank.
    websites = db_session.execute(
        text("SELECT count(*) FROM observations WHERE field = 'website'")
    ).scalar()
    names = db_session.execute(
        text("SELECT count(*) FROM observations WHERE field = 'name'")
    ).scalar()
    assert websites < names


# --- detail extraction ---------------------------------------------------


def test_detail_extraction_adds_fields_the_list_lacks(db_session, clean):
    written = saras.extract_detail(
        db_session,
        DETAIL_HTML,
        entity_key="cbse:330801",
        fetch_id=2,
        content_hash="h_detail",
        observed_at=WHEN,
    )
    db_session.commit()
    assert written > 0
    got = dict(
        db_session.execute(
            text(
                "SELECT field, value_text FROM observations"
                " WHERE entity_key = 'cbse:330801'"
            )
        ).all()
    )
    assert got["legal_entity_name"] == "Sutara Social Empowerment Mission Trust"
    assert got["year_founded"] == "2010"
    assert got["pincode"] == "854101"
    assert got["board_valid_from"] == "01/04/2022"


def test_detail_extraction_stores_no_principal_qualifications(db_session, clean):
    """COMPLIANCE.md purpose limitation. The page publishes 'M A BED'."""
    saras.extract_detail(
        db_session,
        DETAIL_HTML,
        entity_key="cbse:330801",
        fetch_id=2,
        content_hash="h_detail",
        observed_at=WHEN,
    )
    db_session.commit()
    hits = db_session.execute(
        text("SELECT count(*) FROM observations WHERE value_text ILIKE '%M A BED%'")
    ).scalar()
    assert hits == 0


# --- the ADR-009 payoff --------------------------------------------------


def test_run_makes_no_fetches_and_is_idempotent(db_session, clean):
    """`extract --reextract` must re-run over stored bytes with zero fetches.
    That is what makes improving a parser a minutes-long loop, not a re-crawl."""
    import school_intel.extract.saras as mod

    stored = {"h_list": KA_HTML.encode(), "h_detail": DETAIL_HTML.encode()}
    original = mod.load
    mod.load = lambda _s, h: stored.get(h)
    try:
        fetches_before = db_session.execute(
            text("SELECT count(*) FROM fetches")
        ).scalar()

        first = mod.run(db_session)
        db_session.commit()
        second = mod.run(db_session)
        db_session.commit()

        fetches_after = db_session.execute(
            text("SELECT count(*) FROM fetches")
        ).scalar()
    finally:
        mod.load = original

    assert first.documents == 2
    assert first.observations > 10000
    assert second.observations == 0, "re-extraction must add no duplicate rows"
    assert fetches_after == fetches_before, "extraction must make zero fetches"


def test_extractor_version_is_recorded(db_session, clean):
    saras.extract_list(
        db_session, KA_HTML, fetch_id=1, content_hash="h_list", observed_at=WHEN
    )
    db_session.commit()
    extractors = (
        db_session.execute(text("SELECT DISTINCT extractor FROM observations"))
        .scalars()
        .all()
    )
    assert extractors == [saras.LIST_EXTRACTOR]
    assert "v1" in saras.LIST_EXTRACTOR


def test_observed_at_is_the_fetch_time_not_now(db_session, clean):
    """Freshness logic uses observed_at, so it must reflect the source, not the
    moment we happened to re-parse."""
    saras.extract_list(
        db_session, KA_HTML, fetch_id=1, content_hash="h_list", observed_at=WHEN
    )
    db_session.commit()
    observed = (
        db_session.execute(text("SELECT DISTINCT observed_at FROM observations"))
        .scalars()
        .all()
    )
    assert observed == [WHEN]


# --- bytes Postgres cannot store (M2-4) ---------------------------------


def test_a_nul_byte_in_extracted_text_does_not_abort_the_write(db_session, clean):
    """[VERIFIED M2-4] pdfplumber emits a NUL where a ligature fails to decode -
    "Affiliation-Letter.pdf" came back with one - and Postgres text columns
    cannot hold it. One such byte aborted a 449-school enrichment run at the
    insert and lost the whole batch."""
    from school_intel.extract.observations import Claim, sanitise, write_observations

    dirty = "A" + chr(0) + "liation-Letter.pdf"
    assert sanitise(dirty) == "Aliation-Letter.pdf"

    written = write_observations(
        db_session,
        claims=[Claim("website", dirty, "row: " + dirty)],
        entity_key="cbse:999999",
        source_id="cbse_saras",
        extractor="test:v1",
        observed_at=WHEN,
    )
    db_session.commit()
    assert written == 1

    stored = db_session.execute(
        text("SELECT value_text FROM observations WHERE entity_key = 'cbse:999999'")
    ).scalar()
    assert chr(0) not in stored


def test_tabs_and_newlines_survive_sanitising():
    """Evidence spans are multi-line; only unstorable control bytes go."""
    from school_intel.extract.observations import sanitise

    assert sanitise("a\tb\nc") == "a\tb\nc"
