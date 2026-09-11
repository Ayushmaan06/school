"""M1-5: observations -> canonical institutions."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from school_intel.resolve import build
from school_intel.resolve.conflict import Candidate, NoPolicyError, resolve

RUN_AT = datetime(2026, 9, 5, tzinfo=UTC)
OLDER = datetime(2025, 1, 1, tzinfo=UTC)
NEWER = datetime(2026, 6, 1, tzinfo=UTC)


def _cand(source, value, when=NEWER, obs_id=1):
    return Candidate(
        observation_id=obs_id, source_id=source, value=value, observed_at=when
    )


# --- conflict policy (pure) ----------------------------------------------


def test_priority_order_is_respected_per_field():
    """The school's own site is fresher for leadership (6.8/6.11)..."""
    r = resolve(
        "principal_name",
        [_cand("cbse_saras", "OLD HEAD"), _cand("cbse_mpd", "NEW HEAD")],
    )
    assert r.winner.value == "NEW HEAD"

    """...but NEVER wins on board status - only the awarding board can say."""
    r = resolve(
        "board_status", [_cand("cbse_mpd", "active"), _cand("cbse_saras", "expired")]
    )
    assert r.winner.source_id == "cbse_saras"


def test_a_source_with_no_policy_for_a_field_cannot_win():
    """fee_annual_inr is cbse_mpd only - no other source has fee data at all."""
    r = resolve("fee_annual_inr", [_cand("udise", "50000")])
    assert r.winner is None
    assert "not a permitted source" in r.losers[0][1]


def test_ties_within_one_source_go_to_the_newer_observation():
    r = resolve(
        "name",
        [
            _cand("cbse_saras", "OLD NAME", OLDER, 1),
            _cand("cbse_saras", "NEW NAME", NEWER, 2),
        ],
    )
    assert r.winner.value == "NEW NAME"
    assert "older observed_at" in r.losers[0][1]


def test_losers_are_retained_with_a_reason():
    """The loser is never deleted - it is shown by /institutions/{id}/why."""
    r = resolve("name", [_cand("cbse_saras", "A"), _cand("udise", "B")])
    assert r.winner.value == "A"
    assert len(r.losers) == 1
    loser, why = r.losers[0]
    assert loser.value == "B"
    assert "cbse_saras outranks udise" in why


def test_unknown_field_refuses_rather_than_defaulting():
    """Silently falling back to authority_tier would make a policy decision
    invisibly, which is what Project-Doc 6.11 rejected."""
    with pytest.raises(NoPolicyError, match="no conflict policy"):
        resolve("some_new_field", [_cand("cbse_saras", "x")])


def test_numeric_disagreement_over_20_percent_is_flagged():
    """Do not average and do not drop the loser (6.7)."""
    r = resolve("fee_annual_inr", [_cand("cbse_mpd", "200000", NEWER, 1)])
    assert not r.disputed

    r = resolve(
        "total_enrollment",
        [_cand("udise", "1000", NEWER, 1), _cand("cbse_mpd", "1500", NEWER, 2)],
    )
    assert r.disputed, "50% disagreement must raise a dispute"

    r = resolve(
        "total_enrollment",
        [_cand("udise", "1000", NEWER, 1), _cand("cbse_mpd", "1050", NEWER, 2)],
    )
    assert not r.disputed, "5% disagreement is normal reporting variance"


# --- the rebuild ---------------------------------------------------------


@pytest.fixture
def corpus(db_session):
    db_session.execute(
        text(
            "TRUNCATE observations, institutions, institution_boards, scores, roles,"
            " people, groups, overrides, merge_decisions, review_queue,"
            " fetches, raw_documents RESTART IDENTITY CASCADE"
        )
    )
    db_session.execute(
        text(
            "INSERT INTO source_registry (id, kind, authority_tier, refresh_days)"
            " VALUES ('cbse_saras','registry',1,30), ('cbse_mpd','institution_site',3,180),"
            " ('udise','registry',1,365) ON CONFLICT DO NOTHING"
        )
    )
    rows = [
        ("cbse:1", "name", "GREEN VALLEY SCHOOL", "cbse_saras"),
        ("cbse:1", "state", "KARNATAKA", "cbse_saras"),
        ("cbse:1", "district", "BANGALORE URBAN", "cbse_saras"),
        ("cbse:1", "has_class_12", "true", "cbse_saras"),
        ("cbse:1", "website", "https://www.green.edu.in/", "cbse_saras"),
        ("cbse:1", "principal_name", "Dr. A B Sharma", "cbse_saras"),
        ("cbse:2", "name", "BLUE RIDGE SCHOOL", "cbse_saras"),
        ("cbse:2", "state", "KARNATAKA", "cbse_saras"),
        ("cbse:2", "has_class_12", "false", "cbse_saras"),
    ]
    for key, f, value, source in rows:
        db_session.execute(
            text(
                "INSERT INTO observations (entity_type, entity_key, field, value_text,"
                " source_id, evidence_span, observed_at, extractor, confidence)"
                " VALUES ('institution', :k, :f, :v, :s, :e, :o, 'test:v1', 1.0)"
            ),
            {
                "k": key,
                "f": f,
                "v": value,
                "s": source,
                "e": f"{f}: {value}",
                "o": NEWER,
            },
        )
    db_session.commit()
    yield


def test_rebuild_writes_canonical_rows(db_session, corpus):
    result = build.rebuild(db_session, RUN_AT)
    db_session.commit()
    assert result.institutions == 2

    row = db_session.execute(
        text(
            "SELECT canonical_name, state, district, has_class_12, website,"
            " last_resolved_at FROM institutions WHERE cbse_affiliation_no = '1'"
        )
    ).first()
    assert row[0] == "GREEN VALLEY SCHOOL"
    assert row[3] is True
    assert row[5] == RUN_AT, "last_resolved_at must be the passed-in run_at"


def test_rebuild_normalises_geography_and_domains(db_session, corpus):
    build.rebuild(db_session, RUN_AT)
    db_session.commit()
    row = db_session.execute(
        text("SELECT district, website FROM institutions WHERE cbse_affiliation_no='1'")
    ).first()
    assert row[0] == "bengaluru urban", "Bangalore -> Bengaluru"
    assert row[1] == "green.edu.in", "scheme and www stripped"


def test_observations_are_linked_to_their_institution(db_session, corpus):
    build.rebuild(db_session, RUN_AT)
    db_session.commit()
    unlinked = db_session.execute(
        text("SELECT count(*) FROM observations WHERE institution_id IS NULL")
    ).scalar()
    assert unlinked == 0


def test_boards_are_rows_not_a_column(db_session, corpus):
    build.rebuild(db_session, RUN_AT)
    db_session.commit()
    boards = (
        db_session.execute(text("SELECT DISTINCT board FROM institution_boards"))
        .scalars()
        .all()
    )
    assert boards == ["CBSE"]


def test_search_tsv_is_populated_by_the_resolver(db_session, corpus):
    build.rebuild(db_session, RUN_AT)
    db_session.commit()
    missing = db_session.execute(
        text("SELECT count(*) FROM institutions WHERE search_tsv IS NULL")
    ).scalar()
    assert missing == 0


# The four rebuild-contract properties live in test_rebuild.py, which
# AGENTS.md names as mandatory. This file covers resolver behaviour only.


def test_prune_removes_institutions_with_no_live_observations(db_session, corpus):
    build.rebuild(db_session, RUN_AT)
    db_session.commit()
    assert db_session.execute(text("SELECT count(*) FROM institutions")).scalar() == 2

    db_session.execute(text("DELETE FROM observations WHERE entity_key = 'cbse:2'"))
    db_session.commit()

    build.rebuild(db_session, RUN_AT)
    db_session.commit()
    remaining = (
        db_session.execute(text("SELECT cbse_affiliation_no FROM institutions"))
        .scalars()
        .all()
    )
    assert remaining == ["1"]
