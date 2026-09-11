"""`extract --reextract` must not hand one document to several schools.

`fetches` carries no institution_id, so reextract links a stored document back
to a school by matching the fetch URL against `institutions.website`. That
match is not one-to-one in the real data - 128 institutions share
`narayanaschools.in` and 117 have `gmail.com` recorded as their website - so an
unguarded match writes one school's fee and phone as observations for all of
them. Hard rule 6: missing beats wrong.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from school_intel.extract.mpd import run as mpd_run

WHEN = datetime(2026, 9, 6, tzinfo=UTC)

PAGE = b"""<html><body>
  <h2>Contact Us</h2>
  <p>Phone: 080-25551234</p>
  <p>Email: office@example-school.edu.in</p>
</body></html>"""


@pytest.fixture
def seeded(db_session, tmp_path):
    """Two schools sharing one website, one school owning its own."""
    db_session.execute(
        text("TRUNCATE observations, fetches, raw_documents, institutions CASCADE")
    )
    db_session.execute(
        text(
            "INSERT INTO source_registry (id, kind, authority_tier, refresh_days)"
            " VALUES ('cbse_mpd','website',2,180) ON CONFLICT DO NOTHING"
        )
    )

    def add_school(aff, name, website):
        db_session.execute(
            text(
                "INSERT INTO institutions (canonical_name, institution_type,"
                " cbse_affiliation_no, website, status)"
                " VALUES (:n, 'school', :a, :w, 'active')"
            ),
            {"n": name, "a": aff, "w": website},
        )

    # A chain: two branches, one shared website. Matching on URL cannot tell
    # them apart, which is exactly the case that must be skipped.
    add_school("111001", "Chain School Branch A", "chainschools.in")
    add_school("111002", "Chain School Branch B", "chainschools.in")
    # An ordinary school whose website belongs to it alone.
    add_school("222001", "Solo Public School", "solopublic.edu.in")

    for digest, url in (
        ("h_chain", "https://chainschools.in/disclosure"),
        ("h_solo", "https://solopublic.edu.in/disclosure"),
    ):
        path = tmp_path / digest
        path.write_bytes(PAGE)
        db_session.execute(
            text(
                "INSERT INTO raw_documents (content_hash, storage_path, media_type,"
                " bytes) VALUES (:h, :p, 'text/html', :b)"
            ),
            {"h": digest, "p": str(path), "b": len(PAGE)},
        )
        db_session.execute(
            text(
                "INSERT INTO fetches (source_id, url, content_hash, robots_allowed,"
                " http_status) VALUES ('cbse_mpd', :u, :h, true, 200)"
            ),
            {"u": url, "h": digest},
        )
    db_session.commit()
    return db_session


def entity_keys(session) -> set[str]:
    return {
        row[0]
        for row in session.execute(
            text("SELECT DISTINCT entity_key FROM observations WHERE"
                 " source_id = 'cbse_mpd'")
        )
    }


def test_ambiguous_document_is_skipped_not_guessed(seeded):
    """A document matching two schools must produce observations for neither."""
    mpd_run.reextract(seeded, observed_at=WHEN)
    seeded.commit()

    keys = entity_keys(seeded)
    assert "cbse:111001" not in keys, "chain branch A got another school's document"
    assert "cbse:111002" not in keys, "chain branch B got another school's document"


def test_unambiguous_document_still_extracted(seeded):
    """The guard must not silence the ordinary one-school-one-website case."""
    mpd_run.reextract(seeded, observed_at=WHEN)
    seeded.commit()

    assert "cbse:222001" in entity_keys(seeded)


# --- incremental re-extraction -------------------------------------------


def _marked(session) -> int:
    return session.execute(text("SELECT count(*) FROM extraction_runs")).scalar()


def test_second_pass_reads_nothing(seeded):
    """The whole point: cost is O(new documents), not O(corpus).

    reextract used to re-parse - and re-OCR - every stored document on every
    invocation, so the stage got slower as the corpus grew while the new data
    per run shrank.
    """
    first = mpd_run.reextract(seeded, observed_at=WHEN)
    seeded.commit()
    assert first.considered > 0
    assert _marked(seeded) > 0

    second = mpd_run.reextract(seeded, observed_at=WHEN)
    seeded.commit()
    assert second.considered == 0, "a second pass re-read documents it had read"


def test_a_new_extractor_version_makes_documents_eligible_again(seeded, monkeypatch):
    """ADR-009 still holds: improve a parser, re-read stored bytes, no crawling.

    The version is derived from the tier strings, so bumping one is all it takes
    - there is no separate constant that can be forgotten.
    """
    mpd_run.reextract(seeded, observed_at=WHEN)
    seeded.commit()
    assert mpd_run.reextract(seeded, observed_at=WHEN).considered == 0

    monkeypatch.setattr(mpd_run, "PIPELINE_VERSION", "parser:mpd_table_v2|x|y")
    assert mpd_run.reextract(seeded, observed_at=WHEN).considered > 0


def test_full_ignores_the_markers(seeded):
    """The escape hatch, for a parser edited without its version changing."""
    mpd_run.reextract(seeded, observed_at=WHEN)
    seeded.commit()
    assert mpd_run.reextract(seeded, observed_at=WHEN).considered == 0
    assert mpd_run.reextract(seeded, observed_at=WHEN, full=True).considered > 0


# --- the resolver side of the same boundary ------------------------------


def test_resolver_ignores_cross_attributed_mpd_but_keeps_registry(db_session):
    """Mis-attributed MPD evidence is dropped; a shared registry page is not.

    A `cbse_saras` list page covers a whole state and legitimately produces
    observations for hundreds of schools, so the exclusion must not be a blanket
    "one document, one school" rule.
    """
    from school_intel.resolve.build import _load_observations

    db_session.execute(text("TRUNCATE observations CASCADE"))
    for sid in ("cbse_mpd", "cbse_saras"):
        db_session.execute(
            text(
                "INSERT INTO source_registry (id, kind, authority_tier, refresh_days)"
                " VALUES (:s,'website',2,180) ON CONFLICT DO NOTHING"
            ),
            {"s": sid},
        )

    for digest in ("shared_mpd", "solo_mpd", "state_list"):
        db_session.execute(
            text(
                "INSERT INTO raw_documents (content_hash, storage_path, media_type,"
                " bytes) VALUES (:h, '/tmp/x', 'text/html', 1)"
                " ON CONFLICT DO NOTHING"
            ),
            {"h": digest},
        )

    def observe(entity_key, source_id, content_hash, value):
        db_session.execute(
            text(
                "INSERT INTO observations (entity_type, entity_key, field, value_text,"
                " source_id, content_hash, observed_at, extractor, confidence)"
                " VALUES ('institution', :k, 'phone', :v, :s, :h, :w, 'test', 1.0)"
            ),
            {"k": entity_key, "v": value, "s": source_id, "h": content_hash, "w": WHEN},
        )

    # One school website, two schools - impossible, so it is evidence for neither.
    observe("cbse:900001", "cbse_mpd", "shared_mpd", "080-1111111")
    observe("cbse:900002", "cbse_mpd", "shared_mpd", "080-1111111")
    # One school website, one school - ordinary, keep it.
    observe("cbse:900003", "cbse_mpd", "solo_mpd", "080-2222222")
    # One registry list page covering two schools - normal, keep both.
    observe("cbse:900004", "cbse_saras", "state_list", "080-3333333")
    observe("cbse:900005", "cbse_saras", "state_list", "080-4444444")
    db_session.commit()

    loaded = _load_observations(db_session)

    assert "cbse:900001" not in loaded
    assert "cbse:900002" not in loaded
    assert "cbse:900003" in loaded
    assert "cbse:900004" in loaded, "registry list page must not be excluded"
    assert "cbse:900005" in loaded, "registry list page must not be excluded"


def test_mark_enriched_does_not_stamp_schools_sharing_a_website(seeded):
    """A fetch for one school must not mark its website-twins as visited.

    Being stamped means being skipped for 180 days, so an over-broad match
    silently removes schools from the enrichment queue that nobody ever visited.
    """
    mpd_run.backfill_last_enriched(seeded)
    seeded.commit()

    stamped = {
        row[0]
        for row in seeded.execute(
            text(
                "SELECT cbse_affiliation_no FROM institutions"
                " WHERE last_enriched_at IS NOT NULL"
            )
        )
    }
    assert "111001" not in stamped, "chain branch stamped off a shared-website fetch"
    assert "111002" not in stamped, "chain branch stamped off a shared-website fetch"
    assert "222001" in stamped, "the school actually fetched must still be stamped"
