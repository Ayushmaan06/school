"""Turn stored SARAS documents into observations. Deterministic - no LLM.

Extraction reads `raw_documents`, never the network. That is the payoff for
ADR-009: `cli extract --reextract` re-runs an improved parser over stored bytes
with zero fetches and zero API calls, so tuning is a minutes-long loop.

Both document shapes produce observations:

  list page   name, state, district, address, website, principal_name,
              has_class_12, grade_high, status
  detail page adds year_founded, legal_entity_name, board_valid_*, pincode,
              residential

[VERIFIED M0-0] The list page carries most of the value, which is why ADR-017
can make the national import cheap and defer detail work to the campaign.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from school_intel.extract.observations import Claim, write_observations
from school_intel.extract.parsers.cbse_saras import (
    SENIOR_SECONDARY,
    ListRow,
    parse_detail,
    parse_list,
)
from school_intel.fetch.store import load

log = logging.getLogger(__name__)

SOURCE_ID = "cbse_saras"
LIST_EXTRACTOR = "parser:cbse_saras_list_v1"
DETAIL_EXTRACTOR = "parser:cbse_saras_detail_v1"

_DETAIL_URL = re.compile(r"/AfflicationDetails/(\d+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ExtractResult:
    documents: int
    observations: int
    entities: int


def list_claims(row: ListRow) -> list[Claim]:
    """One claim per populated field. A blank field emits nothing (hard rule 6).

    Evidence is the row's own text for that field. It is short here because the
    list is tabular; that is fine - evidence must be verbatim, not lengthy.
    """
    claims: list[Claim] = []

    def add(field: str, value: str | None, label: str) -> None:
        if value:
            claims.append(Claim(field=field, value=value, evidence=f"{label}: {value}"))

    add("name", row.name, "Name")
    add("state", row.state, "State")
    add("district", row.district, "District")
    add("address", row.address, "Address")
    add("website", row.website, "Website")
    add("principal_name", row.principal_name, "Head/Principal Name")

    if row.status:
        claims.append(
            Claim(
                field="grade_high", value=row.status, evidence=f"Status: {row.status}"
            )
        )
        claims.append(
            Claim(
                field="has_class_12",
                value="true" if row.status == SENIOR_SECONDARY else "false",
                evidence=f"Status: {row.status}",
            )
        )
    # The closed list is the only source that says an institution is gone.
    if row.affiliation_status and "disaffiliat" in row.affiliation_status.lower():
        claims.append(
            Claim(
                field="status",
                value="disaffiliated",
                evidence=f"Affiliation Status: {row.affiliation_status}",
            )
        )
    return claims


def extract_list(
    session: Session,
    html: str,
    *,
    fetch_id: int,
    content_hash: str,
    observed_at: datetime,
) -> tuple[int, int]:
    page = parse_list(html)
    written = 0
    for row in page.rows:
        written += write_observations(
            session,
            claims=list_claims(row),
            entity_key=row.entity_key,
            source_id=SOURCE_ID,
            extractor=LIST_EXTRACTOR,
            observed_at=observed_at,
            fetch_id=fetch_id,
            content_hash=content_hash,
        )
    return written, len(page.rows)


def extract_detail(
    session: Session,
    html: str,
    *,
    entity_key: str,
    fetch_id: int,
    content_hash: str,
    observed_at: datetime,
) -> int:
    parsed = parse_detail(html)
    claims = [
        Claim(field=field, value=value, evidence=parsed.evidence[field])
        for field, value in parsed.values.items()
    ]
    return write_observations(
        session,
        claims=claims,
        entity_key=entity_key,
        source_id=SOURCE_ID,
        extractor=DETAIL_EXTRACTOR,
        observed_at=observed_at,
        fetch_id=fetch_id,
        content_hash=content_hash,
    )


def run(session: Session, limit: int | None = None) -> ExtractResult:
    """Extract every stored SARAS document. Makes no HTTP requests at all.

    Re-running is safe and cheap: the partial unique index absorbs repeats, so
    this is also the `--reextract` path.
    """
    rows = session.execute(
        text(
            "SELECT f.id, f.url, f.content_hash, f.requested_at"
            " FROM fetches f"
            " WHERE f.source_id = :s AND f.content_hash IS NOT NULL"
            "   AND f.http_status = 200"
            " ORDER BY f.id" + (" LIMIT :lim" if limit else "")
        ),
        {"s": SOURCE_ID, **({"lim": limit} if limit else {})},
    ).all()

    documents = observations = entities = 0
    for fetch_id, url, content_hash, requested_at in rows:
        body = load(session, content_hash)
        if body is None:
            log.warning(
                "fetch %s: raw document %s is gone; skipping",
                fetch_id,
                content_hash[:12],
            )
            continue
        html = body.decode("utf-8", "replace")
        documents += 1

        detail = _DETAIL_URL.search(url)
        if detail:
            observations += extract_detail(
                session,
                html,
                entity_key=f"cbse:{detail.group(1)}",
                fetch_id=fetch_id,
                content_hash=content_hash,
                observed_at=requested_at,
            )
            entities += 1
        else:
            written, seen = extract_list(
                session,
                html,
                fetch_id=fetch_id,
                content_hash=content_hash,
                observed_at=requested_at,
            )
            observations += written
            entities += seen

    return ExtractResult(
        documents=documents, observations=observations, entities=entities
    )
