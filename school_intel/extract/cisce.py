"""Turn stored CISCE locator documents into observations. Deterministic - no LLM.

Like every extractor, this reads `raw_documents` and never the network (ADR-009),
so `cli extract --reextract` re-runs an improved parser over stored bytes for
free.

There is only one document shape: the locator result page. CISCE publishes no
detail page and no contacts - phone, email, fee and strength all have to come
from S2 (the school's own site). A CISCE-only school is not bound by the CBSE
disclosure proforma, so expect the generic homepage-mining path in
`extract/mpd/run.py` to do that work rather than an Appendix-IX parse.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from school_intel.extract.observations import Claim, write_observations
from school_intel.extract.parsers.cisce import ISC, SchoolCard, parse_result
from school_intel.fetch.store import load

log = logging.getLogger(__name__)

SOURCE_ID = "cisce"
EXTRACTOR = "parser:cisce_locator_v1"


@dataclass(frozen=True, slots=True)
class ExtractResult:
    documents: int
    observations: int
    entities: int


def card_claims(card: SchoolCard) -> list[Claim]:
    """One claim per populated field. A blank field emits nothing (hard rule 6)."""
    claims: list[Claim] = []

    def add(field: str, value: str | None, label: str) -> None:
        if value:
            claims.append(Claim(field=field, value=value, evidence=f"{label}: {value}"))

    add("name", card.name, "Name")
    add("state", card.state, "State")
    add("district", card.district, "District")
    add("address", card.address, "Address")
    add("pincode", card.pincode, "Pin Code")
    add("website", card.website, "Website")
    add("principal_name", card.principal_name, "Principal")
    add("gender", card.gender, "School Type")
    add("residential", card.residential, "School Classification")

    # The hard gate. ISC is class 12; ICSE alone stops at class 10. The evidence
    # names every badge on the card so the negative case is auditable too.
    if card.affiliations:
        badges = ", ".join(card.affiliations)
        claims.append(
            Claim(
                field="has_class_12",
                value="true" if card.has_class_12 else "false",
                evidence=f"Affiliation: {badges}",
            )
        )
        if card.has_class_12:
            claims.append(
                Claim(field="grade_high", value="12", evidence=f"Affiliation: {badges}")
            )
    return claims


def extract_result(
    session: Session,
    html: str,
    *,
    fetch_id: int,
    content_hash: str,
    observed_at: datetime,
) -> tuple[int, int]:
    page = parse_result(html)
    if page.skipped:
        log.warning(
            "fetch %s: %d cards unparsed out of %d",
            fetch_id,
            len(page.skipped),
            page.seen,
        )
    written = 0
    for card in page.rows:
        written += write_observations(
            session,
            claims=card_claims(card),
            entity_key=card.entity_key,
            source_id=SOURCE_ID,
            extractor=EXTRACTOR,
            observed_at=observed_at,
            fetch_id=fetch_id,
            content_hash=content_hash,
        )
    return written, len(page.rows)


def run(session: Session, limit: int | None = None) -> ExtractResult:
    """Extract every stored CISCE document. Makes no HTTP requests at all."""
    rows = session.execute(
        text(
            "SELECT f.id, f.content_hash, f.requested_at"
            " FROM fetches f"
            " WHERE f.source_id = :s AND f.content_hash IS NOT NULL"
            "   AND f.http_status = 200"
            " ORDER BY f.id" + (" LIMIT :lim" if limit else "")
        ),
        {"s": SOURCE_ID, **({"lim": limit} if limit else {})},
    ).all()

    documents = observations = entities = 0
    for fetch_id, content_hash, requested_at in rows:
        body = load(session, content_hash)
        if body is None:
            log.warning(
                "fetch %s: raw document %s is gone; skipping",
                fetch_id,
                content_hash[:12],
            )
            continue
        documents += 1
        written, seen = extract_result(
            session,
            body.decode("utf-8", "replace"),
            fetch_id=fetch_id,
            content_hash=content_hash,
            observed_at=requested_at,
        )
        observations += written
        entities += seen

    return ExtractResult(
        documents=documents, observations=observations, entities=entities
    )


__all__ = ["EXTRACTOR", "ISC", "SOURCE_ID", "ExtractResult", "card_claims", "run"]
