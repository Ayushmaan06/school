"""M0-2: the schema is a specification. Assert it round-trips and matches the spec.

Uses its own throwaway database, because it downgrades to base - which would
destroy the shared `engine` fixture other integration tests rely on.
"""

import pytest
from sqlalchemy import create_engine, inspect, text

from school_intel.models import APPEND_ONLY_TABLES, CANONICAL_TABLES

from .conftest import drop_database, make_database, migrate, postgres_reachable

# Every table in docs/DATA-MODEL.md.
EXPECTED_TABLES = {
    "source_registry",
    "fetches",
    "raw_documents",
    "observations",
    "institutions",
    "institution_boards",
    "groups",
    "people",
    "roles",
    "scores",
    "signals",
    "registry_snapshots",
    "jobs",
    "review_queue",
    "overrides",
    "merge_decisions",
    "pipeline_runs",
    "denylist_learned",
    "eval_gold",
}

DB_NAME = "school_intel_migtest"


@pytest.fixture(scope="module")
def mig():
    if not postgres_reachable():
        pytest.skip("no Postgres")
    url = make_database(DB_NAME)
    migrate(url)
    eng = create_engine(url)
    yield eng, url
    eng.dispose()
    drop_database(DB_NAME)


def test_every_spec_table_exists(mig):
    eng, _ = mig
    missing = EXPECTED_TABLES - set(inspect(eng).get_table_names())
    assert not missing, (
        f"tables in docs/DATA-MODEL.md missing from the migration: {missing}"
    )


def test_layer_table_lists_are_real_tables(mig):
    eng, _ = mig
    got = set(inspect(eng).get_table_names())
    assert set(CANONICAL_TABLES) <= got
    assert set(APPEND_ONLY_TABLES) <= got


def test_pg_trgm_enabled(mig):
    eng, _ = mig
    with eng.connect() as c:
        assert (
            c.execute(
                text("SELECT 1 FROM pg_extension WHERE extname='pg_trgm'")
            ).scalar()
            == 1
        )


def test_partial_unique_index_on_observations(mig):
    """Alembic autogenerate misses partial indexes and this one is load-bearing:
    it is what makes re-extraction idempotent."""
    eng, _ = mig
    with eng.connect() as c:
        ddl = c.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE indexname='uq_observations_live'"
            )
        ).scalar()
    assert ddl is not None, "partial unique index on observations is missing"
    assert "UNIQUE" in ddl
    assert "superseded_by IS NULL" in ddl


def test_fee_mid_is_generated(mig):
    """Scoring and the fee-sorted list must never disagree about "the fee"."""
    eng, _ = mig
    with eng.connect() as c:
        gen = c.execute(
            text(
                "SELECT is_generated FROM information_schema.columns "
                "WHERE table_name='institutions' AND column_name='fee_annual_inr_mid'"
            )
        ).scalar()
    assert gen == "ALWAYS"


def test_fee_mid_computes_midpoint_and_handles_nulls(mig):
    eng, _ = mig
    with eng.begin() as c:
        c.execute(text("TRUNCATE institutions CASCADE"))
        c.execute(
            text(
                "INSERT INTO institutions (id, canonical_name, institution_type,"
                " fee_annual_inr_min, fee_annual_inr_max) VALUES"
                " (1,'both','school',100000,200000),"
                " (2,'min only','school',150000,NULL),"
                " (3,'max only','school',NULL,180000),"
                " (4,'neither','school',NULL,NULL)"
            )
        )
        rows = dict(
            c.execute(
                text("SELECT id, fee_annual_inr_mid FROM institutions ORDER BY id")
            ).all()
        )
    assert rows == {1: 150000, 2: 150000, 3: 180000, 4: None}


def test_downgrade_to_base_leaves_no_spec_tables(mig):
    """Runs last: it empties this module's database."""
    import os

    from alembic import command

    from school_intel import db
    from school_intel.config import get_settings

    eng, url = mig
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    db.get_engine.cache_clear()
    try:
        command.downgrade(db._alembic_config(), "base")
        remaining = set(inspect(eng).get_table_names()) & EXPECTED_TABLES
        assert not remaining, f"downgrade left tables behind: {remaining}"
        command.upgrade(db._alembic_config(), "head")
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old
        get_settings.cache_clear()
        db.get_engine.cache_clear()
