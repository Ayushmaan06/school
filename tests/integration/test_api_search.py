"""Name search: the acronym the BD team actually types must find the school."""

from sqlalchemy import text

from school_intel.api import queries

NAMES = [
    ("DELHI PUBLIC SCHOOL", "BENGALURU", "KARNATAKA"),
    ("KENDRIYA VIDYALAYA", "PUNE", "MAHARASHTRA"),
    ("ST JOSEPHS BOYS HIGH SCHOOL", "BENGALURU", "KARNATAKA"),
]


def _seed(db_session):
    for name, city, state in NAMES:
        db_session.execute(
            text(
                "INSERT INTO institutions (canonical_name, institution_type, city,"
                " state, has_class_12, search_tsv) VALUES (:n, 'school', :c, :s, true,"
                " to_tsvector('simple', :n || ' ' || :c || ' ' || :s))"
            ),
            {"n": name, "c": city, "s": state},
        )
    db_session.commit()


def _names(rows):
    return [r["canonical_name"] for r in rows]


def test_acronym_finds_the_chain(db_session):
    """similarity('DELHI PUBLIC SCHOOL', 'dps') is 0.04 - only the alias bridges it."""
    _seed(db_session)
    assert "DELHI PUBLIC SCHOOL" in _names(queries.schools_by_name(db_session, "dps"))
    assert "KENDRIYA VIDYALAYA" in _names(queries.schools_by_name(db_session, "kv"))


def test_full_name_and_place_words_both_narrow(db_session):
    _seed(db_session)
    assert set(_names(queries.schools_by_name(db_session, "delhi public school"))) == {
        "DELHI PUBLIC SCHOOL"
    }
    # search_tsv carries the city, so a place word filters a chain to one campus.
    assert set(
        _names(queries.schools_by_name(db_session, "kendriya vidyalaya pune"))
    ) == {"KENDRIYA VIDYALAYA"}
    # ...and the wrong city finds nothing, so the filter is real.
    assert queries.schools_by_name(db_session, "kendriya vidyalaya chennai") == []


def test_single_word_typo_still_matches_via_trigram(db_session):
    _seed(db_session)
    assert "KENDRIYA VIDYALAYA" in _names(
        queries.schools_by_name(db_session, "vidhyalaya")
    )


def test_trigram_fallback_never_defeats_place_narrowing(db_session):
    """The whole reason the fallback is single-word only: a trigram match
    ignores 'chennai' entirely and would return the Pune campus."""
    _seed(db_session)
    assert queries.schools_by_name(db_session, "kendriya vidyalaya chennai") == []


def test_no_match_returns_empty_not_everything(db_session):
    _seed(db_session)
    assert queries.schools_by_name(db_session, "zzzqqq") == []
