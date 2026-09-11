"""M0-4: the seed is idempotent, and code cannot reference a source that is not in it."""

from sqlalchemy import text

from school_intel.sources import SOURCE_IDS, load_seed, seed_sources


def test_seed_matches_the_declared_source_ids():
    """A typo in a source_id should fail here, not as a foreign key at 3am."""
    assert {row["id"] for row in load_seed()} == set(SOURCE_IDS)


def test_seed_is_idempotent(db_session):
    db_session.execute(text("TRUNCATE source_registry CASCADE"))
    db_session.commit()

    assert seed_sources(db_session) == 9
    db_session.commit()
    first = db_session.execute(
        text("SELECT id, authority_tier, refresh_days FROM source_registry ORDER BY id")
    ).all()

    seed_sources(db_session)
    db_session.commit()
    second = db_session.execute(
        text("SELECT id, authority_tier, refresh_days FROM source_registry ORDER BY id")
    ).all()

    assert first == second
    assert (
        db_session.execute(text("SELECT count(*) FROM source_registry")).scalar() == 9
    )


def test_documented_tiers_and_cadences(db_session):
    seed_sources(db_session)
    db_session.commit()
    rows = dict(
        db_session.execute(text("SELECT id, authority_tier FROM source_registry")).all()
    )
    # docs/SOURCES.md: 1=govt/statutory, 2=board/official, 3=self-published, 4=third party
    assert rows["cbse_saras"] == 1
    assert rows["udise"] == 1
    assert rows["pin_centroids"] == 1
    assert rows["cisce"] == rows["ib"] == rows["cambridge"] == 2
    assert rows["cbse_mpd"] == 3
    assert rows["group_directory"] == 3
    assert rows["serper"] == 4


def test_gov_sources_are_rate_limited_well_below_one_per_second(db_session):
    """*.gov.in is government infrastructure and Project-Doc 6.12 was right that
    per-domain limits matter.

    cbse_saras is 0.1, not 0.5: [VERIFIED M1-3] 0.5 rps triggered a CAPTCHA wall
    after roughly 150 detail pages and yielded only 124 of 539.
    """
    seed_sources(db_session)
    db_session.commit()
    rows = dict(
        db_session.execute(text("SELECT id, rate_limit_rps FROM source_registry")).all()
    )
    assert float(rows["cbse_saras"]) == 0.1
    assert float(rows["udise"]) == 0.5
    assert all(float(v) <= 0.5 for k, v in rows.items() if k in {"cbse_saras", "udise"})


def test_serper_is_seeded_but_disabled(db_session):
    """[VERIFIED M0-0] 99.1% website coverage leaves this source almost nothing
    to do. Enabling it should be a decision, not a default."""
    seed_sources(db_session)
    db_session.commit()
    enabled = db_session.execute(
        text("SELECT enabled FROM source_registry WHERE id='serper'")
    ).scalar()
    assert enabled is False
