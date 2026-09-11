"""Shared Postgres fixtures for integration tests.

Integration tests need a real database: SKIP LOCKED, partial indexes and
generated columns are exactly the things a fake would not reproduce. When no
Postgres is reachable the whole directory skips, so `pytest` still passes on a
bare checkout.
"""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

BASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://school_intel:school_intel@localhost:5433/school_intel_test",
)
ADMIN_URL = BASE_URL.rsplit("/", 1)[0] + "/postgres"


def postgres_reachable(url: str = ADMIN_URL) -> bool:
    try:
        create_engine(url).connect().close()
        return True
    except Exception:  # noqa: BLE001 - any connect failure means "skip"
        return False


def make_database(name: str) -> str:
    """Drop and recreate `name`, returning its URL."""
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        c.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    return BASE_URL.rsplit("/", 1)[0] + f"/{name}"


def drop_database(name: str) -> None:
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def migrate(url: str) -> None:
    """Run alembic against `url` without leaking it into other tests' settings."""
    from alembic import command

    from school_intel import db
    from school_intel.config import get_settings

    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    db.get_engine.cache_clear()
    try:
        command.upgrade(db._alembic_config(), "head")
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old
        get_settings.cache_clear()
        db.get_engine.cache_clear()


pytestmark = pytest.mark.skipif(
    not postgres_reachable(), reason="no Postgres; run `make db`"
)


def pytest_collection_modifyitems(config, items):
    if postgres_reachable():
        return
    skip = pytest.mark.skip(reason="no Postgres; run `make db`")
    for item in items:
        item.add_marker(skip)


@pytest.fixture(scope="session")
def engine():
    """A migrated scratch database, shared by every integration test."""
    if not postgres_reachable():
        pytest.skip("no Postgres")
    name = "school_intel_test"
    url = make_database(name)
    migrate(url)
    eng = create_engine(url)
    yield eng
    eng.dispose()
    drop_database(name)


@pytest.fixture
def db_session(engine):
    with Session(engine) as session:
        yield session
