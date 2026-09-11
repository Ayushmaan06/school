"""MANDATORY (AGENTS.md). The load-bearing test of the architecture.

The four properties from docs/DATA-MODEL.md "Rebuild contract":

  (a) deterministic       - identical inputs produce identical canonical rows
  (b) override-preserving - every overrides row survives
  (c) merge-preserving    - every merge_decisions row is honoured
  (d) non-destructive     - the append-only tables are never touched

ADR-009: if rebuild is not trustworthy, no later improvement is safe. Property
(d) is not hypothetical - implementing the contract's original wording
(`TRUNCATE institutions ... CASCADE`) deleted 17,628 observations, because
CASCADE ignores ON DELETE rules. This file is what catches that class of bug.

The corpus below is deliberately adversarial: two sources disagreeing on a
numeric field, a conflicting name, an override, a merge pair, an institution
that will be pruned, and rows in every append-only table so (d) has something
to protect.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from school_intel.models import APPEND_ONLY_TABLES
from school_intel.resolve import build

RUN_AT = datetime(2026, 9, 5, tzinfo=UTC)
OLDER = datetime(2025, 3, 1, tzinfo=UTC)
NEWER = datetime(2026, 6, 1, tzinfo=UTC)

# The canonical columns a rebuild must reproduce byte-identically.
SNAPSHOT_SQL = text(
    "SELECT cbse_affiliation_no, udise_code, canonical_name, institution_type,"
    " address, pincode, city, district, state, has_class_12, website, email, phone,"
    " total_enrollment, legal_entity_name, legal_entity_norm, status, year_founded"
    " FROM institutions ORDER BY coalesce(cbse_affiliation_no, udise_code)"
)


def _obs(session, key, field, value, source, when=NEWER):
    session.execute(
        text(
            "INSERT INTO observations (entity_type, entity_key, field, value_text,"
            " source_id, evidence_span, observed_at, extractor, confidence)"
            " VALUES ('institution', :k, :f, :v, :s, :e, :o, 'test:v1', 1.0)"
        ),
        {
            "k": key,
            "f": field,
            "v": value,
            "s": source,
            "e": f"{field}: {value}",
            "o": when,
        },
    )


@pytest.fixture
def adversarial_corpus(db_session):
    db_session.execute(
        text(
            "TRUNCATE observations, institutions, institution_boards, scores, roles,"
            " people, groups, overrides, merge_decisions, review_queue, signals,"
            " registry_snapshots, fetches, raw_documents, eval_gold, denylist_learned"
            " RESTART IDENTITY CASCADE"
        )
    )
    db_session.execute(
        text(
            "INSERT INTO source_registry (id, kind, authority_tier, refresh_days)"
            " VALUES ('cbse_saras','registry',1,30), ('cbse_mpd','institution_site',3,180),"
            " ('udise','registry',1,365) ON CONFLICT DO NOTHING"
        )
    )

    # Layer 1: something for property (d) to protect.
    db_session.execute(
        text(
            "INSERT INTO raw_documents (content_hash, storage_path, media_type, bytes)"
            " VALUES ('h1','/tmp/h1','text/html',10), ('h2','/tmp/h2','application/pdf',20)"
        )
    )
    db_session.execute(
        text(
            "INSERT INTO fetches (source_id, url, content_hash, http_status, robots_allowed)"
            " VALUES ('cbse_saras','https://x.test/a','h1',200,true),"
            "        ('cbse_mpd','https://y.test/b','h2',200,true)"
        )
    )
    db_session.execute(
        text(
            "INSERT INTO registry_snapshots (source_id, row_count, content_hash)"
            " VALUES ('cbse_saras', 1847, 'h1')"
        )
    )
    db_session.execute(
        text(
            "INSERT INTO eval_gold (institution_key, field, expected_json, verified_by,"
            " verified_at) VALUES ('cbse:1','fee_annual_inr','185000'::jsonb,'ayushmaan',"
            " DATE '2026-09-01')"
        )
    )
    db_session.execute(
        text(
            "INSERT INTO denylist_learned (url, reason, added_by)"
            " VALUES ('https://x.test/results.pdf','student_data_detected','extractor')"
        )
    )

    # Layer 2.
    _obs(db_session, "cbse:1", "name", "GREEN VALLEY SCHOOL", "cbse_saras")
    _obs(
        db_session, "cbse:1", "name", "Green Valley Sr Sec", "cbse_mpd"
    )  # loses on priority
    _obs(db_session, "cbse:1", "state", "KARNATAKA", "cbse_saras")
    _obs(db_session, "cbse:1", "district", "BANGALORE URBAN", "cbse_saras")
    _obs(db_session, "cbse:1", "has_class_12", "true", "cbse_saras")
    _obs(
        db_session,
        "cbse:1",
        "legal_entity_name",
        "Green Valley Trust (Regd.)",
        "cbse_saras",
    )
    # Two sources disagreeing by >20% - must raise a conflict, never average.
    _obs(db_session, "cbse:1", "total_enrollment", "1000", "udise")
    _obs(db_session, "cbse:1", "total_enrollment", "1600", "cbse_mpd")
    # Same source, two ages - newer must win.
    _obs(db_session, "cbse:1", "phone", "080-11110000", "cbse_mpd", OLDER)
    _obs(db_session, "cbse:1", "phone", "080-22223333", "cbse_mpd", NEWER)

    _obs(db_session, "cbse:2", "name", "BLUE RIDGE SCHOOL", "cbse_saras")
    _obs(db_session, "cbse:2", "state", "KARNATAKA", "cbse_saras")

    _obs(db_session, "cbse:3", "name", "TO BE PRUNED SCHOOL", "cbse_saras")
    _obs(db_session, "cbse:3", "state", "KERALA", "cbse_saras")

    db_session.commit()
    yield


def _append_only_counts(session) -> dict[str, int]:
    return {
        table: session.execute(text(f"SELECT count(*) FROM {table}")).scalar()
        for table in APPEND_ONLY_TABLES
    }


# --- (d) non-destructive. First, because it is the one that bit us. -------


def test_d_rebuild_never_touches_the_append_only_tables(db_session, adversarial_corpus):
    """`TRUNCATE institutions CASCADE` looks correct and deletes observations
    and signals. CASCADE ignores ON DELETE rules and truncates every referencing
    table. This assertion is what turns that into a two-second failure."""
    before = _append_only_counts(db_session)
    assert before["observations"] > 0
    assert before["raw_documents"] > 0
    assert before["fetches"] > 0

    build.rebuild(db_session, RUN_AT)
    db_session.commit()
    build.rebuild(db_session, RUN_AT)
    db_session.commit()

    after = _append_only_counts(db_session)
    for table in APPEND_ONLY_TABLES:
        if table == "review_queue":
            # Conflicts are appended, never removed - it may only grow.
            assert after[table] >= before[table], f"{table} lost rows"
        else:
            assert after[table] == before[table], (
                f"rebuild changed {table}: {before[table]} -> {after[table]} (hard rule 4)"
            )


def test_d_raw_documents_survive_and_still_back_reextraction(
    db_session, adversarial_corpus
):
    """Recovery from the CASCADE incident only worked because these survived."""
    build.rebuild(db_session, RUN_AT)
    db_session.commit()
    hashes = (
        db_session.execute(text("SELECT content_hash FROM raw_documents ORDER BY 1"))
        .scalars()
        .all()
    )
    assert hashes == ["h1", "h2"]


# --- (a) deterministic ----------------------------------------------------


def test_a_two_rebuilds_produce_identical_canonical_rows(
    db_session, adversarial_corpus
):
    build.rebuild(db_session, RUN_AT)
    db_session.commit()
    first = db_session.execute(SNAPSHOT_SQL).all()

    build.rebuild(db_session, RUN_AT)
    db_session.commit()
    second = db_session.execute(SNAPSHOT_SQL).all()

    assert first == second
    assert len(first) == 3


def test_a_determinism_holds_with_a_different_run_at(db_session, adversarial_corpus):
    """run_at is passed in, so only the timestamp columns may differ - the
    resolved values themselves must not move."""
    build.rebuild(db_session, RUN_AT)
    db_session.commit()
    first = db_session.execute(SNAPSHOT_SQL).all()

    build.rebuild(db_session, datetime(2027, 1, 1, tzinfo=UTC))
    db_session.commit()
    second = db_session.execute(SNAPSHOT_SQL).all()
    assert first == second


def test_a_resolution_outcomes_are_stable(db_session, adversarial_corpus):
    """The adversarial cases must resolve the same way every time."""
    build.rebuild(db_session, RUN_AT)
    db_session.commit()
    row = db_session.execute(
        text(
            "SELECT canonical_name, phone, total_enrollment, legal_entity_norm"
            " FROM institutions WHERE cbse_affiliation_no = '1'"
        )
    ).first()
    assert row[0] == "GREEN VALLEY SCHOOL", "cbse_saras outranks cbse_mpd for name"
    assert row[1] == "080-22223333", "newer observation wins within one source"
    assert row[2] == 1000, "udise outranks cbse_mpd for total_enrollment"
    assert row[3] is not None


def test_a_numeric_disagreement_raises_a_conflict_rather_than_averaging(
    db_session, adversarial_corpus
):
    result = build.rebuild(db_session, RUN_AT)
    db_session.commit()
    assert result.conflicts >= 1

    enrollment = db_session.execute(
        text("SELECT total_enrollment FROM institutions WHERE cbse_affiliation_no='1'")
    ).scalar()
    assert enrollment == 1000, "must pick a side, never the 1300 average"

    kinds = (
        db_session.execute(text("SELECT DISTINCT kind FROM review_queue"))
        .scalars()
        .all()
    )
    assert "conflict" in kinds


# --- (b) override-preserving ---------------------------------------------


def test_b_every_override_is_reflected_after_a_rebuild(db_session, adversarial_corpus):
    build.rebuild(db_session, RUN_AT)
    db_session.commit()
    ids = dict(
        db_session.execute(
            text("SELECT cbse_affiliation_no, id FROM institutions")
        ).all()
    )

    db_session.execute(
        text(
            "INSERT INTO overrides (entity_type, entity_id, field, value_json, reason, author)"
            " VALUES ('institution', :a, 'canonical_name',"
            "         to_jsonb('HUMAN CORRECTED NAME'::text), 'registry typo', 'ayushmaan'),"
            "        ('institution', :b, 'city', to_jsonb('mysuru'::text),"
            "         'registry district is wrong', 'ayushmaan')"
        ),
        {"a": ids["1"], "b": ids["2"]},
    )
    db_session.commit()

    for _ in range(2):
        build.rebuild(db_session, RUN_AT)
        db_session.commit()

    assert (
        db_session.execute(
            text("SELECT canonical_name FROM institutions WHERE id = :i"),
            {"i": ids["1"]},
        ).scalar()
        == "HUMAN CORRECTED NAME"
    )
    assert (
        db_session.execute(
            text("SELECT city FROM institutions WHERE id = :i"), {"i": ids["2"]}
        ).scalar()
        == "mysuru"
    )


def test_b_an_override_beats_the_winning_source_not_just_the_losers(
    db_session, adversarial_corpus
):
    build.rebuild(db_session, RUN_AT)
    db_session.commit()
    institution_id = db_session.execute(
        text("SELECT id FROM institutions WHERE cbse_affiliation_no='1'")
    ).scalar()
    db_session.execute(
        text(
            "INSERT INTO overrides (entity_type, entity_id, field, value_json, reason, author)"
            " VALUES ('institution', :i, 'total_enrollment', to_jsonb(1234), 'counted by BD',"
            "         'ayushmaan')"
        ),
        {"i": institution_id},
    )
    db_session.commit()

    build.rebuild(db_session, RUN_AT)
    db_session.commit()
    assert (
        db_session.execute(
            text("SELECT total_enrollment FROM institutions WHERE id = :i"),
            {"i": institution_id},
        ).scalar()
        == 1234
    )


def test_b_overrides_themselves_are_never_consumed(db_session, adversarial_corpus):
    """Applying an override must not delete it - it has to survive every future
    rebuild, not just the next one."""
    build.rebuild(db_session, RUN_AT)
    db_session.commit()
    institution_id = db_session.execute(
        text("SELECT id FROM institutions WHERE cbse_affiliation_no='1'")
    ).scalar()
    db_session.execute(
        text(
            "INSERT INTO overrides (entity_type, entity_id, field, value_json, reason, author)"
            " VALUES ('institution', :i, 'canonical_name', to_jsonb('X'::text), 'r', 'a')"
        ),
        {"i": institution_id},
    )
    db_session.commit()

    for _ in range(3):
        build.rebuild(db_session, RUN_AT)
        db_session.commit()

    assert db_session.execute(text("SELECT count(*) FROM overrides")).scalar() == 1


# --- (c) merge-preserving -------------------------------------------------


def test_c_merge_decisions_are_honoured(db_session, adversarial_corpus):
    db_session.execute(
        text(
            "INSERT INTO merge_decisions (entity_key_a, entity_key_b, decision, decided_by)"
            " VALUES ('cbse:2','cbse:1','same','ayushmaan')"
        )
    )
    db_session.commit()

    result = build.rebuild(db_session, RUN_AT)
    db_session.commit()
    assert result.institutions == 2, "3 entities, one merged pair -> 2 canonical rows"

    linked = db_session.execute(
        text(
            "SELECT count(DISTINCT institution_id) FROM observations"
            " WHERE entity_key IN ('cbse:1','cbse:2')"
        )
    ).scalar()
    assert linked == 1, "both members must point at one institution"


def test_c_merge_is_stable_across_rebuilds(db_session, adversarial_corpus):
    """The primary is the lowest key, chosen deterministically - never insertion
    order, or two rebuilds would disagree about which row survives."""
    db_session.execute(
        text(
            "INSERT INTO merge_decisions (entity_key_a, entity_key_b, decision, decided_by)"
            " VALUES ('cbse:2','cbse:1','same','ayushmaan')"
        )
    )
    db_session.commit()

    build.rebuild(db_session, RUN_AT)
    db_session.commit()
    first = db_session.execute(SNAPSHOT_SQL).all()
    build.rebuild(db_session, RUN_AT)
    db_session.commit()
    assert db_session.execute(SNAPSHOT_SQL).all() == first


def test_c_a_different_decision_is_not_a_merge(db_session, adversarial_corpus):
    db_session.execute(
        text(
            "INSERT INTO merge_decisions (entity_key_a, entity_key_b, decision, decided_by)"
            " VALUES ('cbse:1','cbse:2','different','ayushmaan')"
        )
    )
    db_session.commit()
    result = build.rebuild(db_session, RUN_AT)
    db_session.commit()
    assert result.institutions == 3


# --- id stability, which property (d) depends on -------------------------


def test_institution_ids_survive_a_rebuild(db_session, adversarial_corpus):
    """signals.institution_id is ON DELETE CASCADE. Recreating institution rows
    every run would silently discard the signal history M4-3 accumulates."""
    build.rebuild(db_session, RUN_AT)
    db_session.commit()
    institution_id = db_session.execute(
        text("SELECT id FROM institutions WHERE cbse_affiliation_no='1'")
    ).scalar()
    db_session.execute(
        text(
            "INSERT INTO signals (institution_id, signal_type, source_id, event_date)"
            " VALUES (:i, 'principal_change', 'cbse_saras', DATE '2026-08-01')"
        ),
        {"i": institution_id},
    )
    db_session.commit()

    build.rebuild(db_session, RUN_AT)
    db_session.commit()

    assert (
        db_session.execute(
            text("SELECT id FROM institutions WHERE cbse_affiliation_no='1'")
        ).scalar()
        == institution_id
    )
    assert db_session.execute(text("SELECT count(*) FROM signals")).scalar() == 1
