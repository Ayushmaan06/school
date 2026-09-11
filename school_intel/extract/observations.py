"""Writing observations. Every tier goes through here.

Two invariants this module enforces so callers cannot forget them:

1. **Evidence is mandatory.** An extraction without an `evidence_span` is a bug
   at every tier - a parser records the matched row exactly as a model would
   record its span, because `/institutions/{id}/why` must not care which tier
   produced a value.
2. **Only the canonical field vocabulary.** An unknown field name is rejected
   loudly rather than written and silently ignored by the resolver.

Writes are idempotent against the partial unique index
`(content_hash, extractor, entity_key, field) WHERE superseded_by IS NULL`, so
re-running an extractor over stored documents adds nothing new.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from school_intel.models import OBSERVATION_FIELDS, Observation

# Postgres text columns cannot hold a NUL byte, and pdfplumber emits them when
# a ligature fails to decode: "Affiliation-Letter.pdf" comes back with a NUL
# where the "ffi" should be. [VERIFIED M2-4] one such byte aborted a
# 449-school enrichment run at the insert and lost the whole batch.
#
# Built from code points rather than a regex literal, so the source file never
# contains a control character itself.
_FORBIDDEN = {code: None for code in range(0x20) if code not in (0x09, 0x0A, 0x0D)}
_FORBIDDEN[0x7F] = None


def sanitise(value: str | None) -> str | None:
    """Strip characters Postgres cannot store. Tabs and newlines survive."""
    if value is None:
        return None
    return value.translate(_FORBIDDEN)


class UnknownFieldError(ValueError):
    """A field outside docs/DATA-MODEL.md's canonical vocabulary."""


class MissingEvidenceError(ValueError):
    """An extraction with no evidence span. A bug at every tier."""


@dataclass(frozen=True, slots=True)
class Claim:
    field: str
    value: str
    evidence: str


def write_observations(
    session: Session,
    *,
    claims: list[Claim],
    entity_key: str,
    source_id: str,
    extractor: str,
    observed_at: datetime,
    fetch_id: int | None = None,
    content_hash: str | None = None,
    entity_type: str = "institution",
    confidence: float = 1.0,
) -> int:
    """Insert claims as observations. Returns the number of NEW rows.

    ONE batched statement, not one round-trip per claim. A national import
    writes on the order of 250k observations; per-row inserts turned that into
    minutes of pure network latency. Note that passing a list of params to a
    `text()` construct does NOT batch - it has to be a SQLAlchemy insert() for
    insertmanyvalues to kick in.
    """
    rows = []
    for claim in claims:
        if claim.field not in OBSERVATION_FIELDS:
            raise UnknownFieldError(
                f"{claim.field!r} is not in the canonical field vocabulary. "
                "Adding one requires updating resolve/conflict.py and "
                "docs/SOURCES.md in the same change (docs/DATA-MODEL.md)."
            )
        if not claim.evidence or not claim.evidence.strip():
            raise MissingEvidenceError(
                f"{claim.field!r} has no evidence_span. This breaks "
                "/institutions/{id}/why, which must not care which tier "
                "produced a value (AGENTS.md)."
            )
        rows.append(
            {
                "entity_type": entity_type,
                "entity_key": entity_key,
                "field": claim.field,
                "value_text": sanitise(claim.value),
                "source_id": source_id,
                "fetch_id": fetch_id,
                "content_hash": content_hash,
                "evidence_span": sanitise(claim.evidence)[:4000],
                "observed_at": observed_at,
                "extractor": extractor,
                "confidence": confidence,
            }
        )
    if not rows:
        return 0
    return insert_observation_rows(session, rows)


def insert_observation_rows(session: Session, rows: list[dict]) -> int:
    """Batched idempotent insert. Returns how many rows were actually new.

    RETURNING with ON CONFLICT DO NOTHING yields only the rows that landed, so
    the count is exact rather than optimistic.
    """
    if not rows:
        return 0
    statement = insert(Observation).on_conflict_do_nothing().returning(Observation.id)
    return len(session.execute(statement, rows).scalars().all())
