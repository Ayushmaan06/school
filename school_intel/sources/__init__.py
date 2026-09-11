"""Source registry: the list of things this system is allowed to fetch.

`seed.yaml` mirrors docs/SOURCES.md S1-S9. Seeding is idempotent, so it is safe
to run on every startup.
"""

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from school_intel.models import SourceRegistry

SEED_PATH = Path(__file__).with_name("seed.yaml")

# Every source id the code is allowed to reference. test_source_registry asserts
# the seed and this set agree, so a typo in a source_id fails a test rather than
# a foreign key at 3am.
SOURCE_IDS = frozenset(
    {
        "cbse_saras",
        "cbse_mpd",
        "cisce",
        "ib",
        "cambridge",
        "udise",
        "group_directory",
        "serper",
        "pin_centroids",
    }
)


def load_seed(path: Path = SEED_PATH) -> list[dict[str, Any]]:
    rows = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise TypeError(f"{path} must contain a list of source rows")
    return rows


def seed_sources(session: Session, path: Path = SEED_PATH) -> int:
    """Idempotent upsert. Returns the number of rows written."""
    rows = load_seed(path)
    existing = {s.id: s for s in session.scalars(select(SourceRegistry))}
    for row in rows:
        target = existing.get(row["id"]) or SourceRegistry(id=row["id"])
        for key, value in row.items():
            if key != "id":
                setattr(target, key, value)
        session.add(target)
    session.flush()
    return len(rows)
