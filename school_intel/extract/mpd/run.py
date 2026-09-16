"""Run MPD discovery and extraction over the corpus, and record what happened.

Discovery (M2-1) is a fetch stage; extraction (M2-2, M2-2b) reads stored bytes.
They are separate so `--reextract` re-runs the parsers with zero fetches.

Per-hop outcomes are counted separately on purpose: "homepage unreachable",
"no MPD page", "no fee link" and "fee document unreadable" fail for completely
different reasons and need completely different fixes. One blended coverage
number hides which one is actually costing you.
"""

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from selectolax.parser import HTMLParser
from sqlalchemy import text
from sqlalchemy.orm import Session

from school_intel.extract.mpd import ocr, patterns, tables
from school_intel.extract.mpd.schemas import MPDExtraction
from school_intel.extract.observations import Claim, write_observations
from school_intel.fetch.store import delete_document, load
from school_intel.sources import cbse_mpd
from school_intel.sources.cbse_mpd import SOURCE_ID

log = logging.getLogger(__name__)

# Commit often. [VERIFIED M5] at 25 the fetch rows sat in an uncommitted
# transaction while slow and dead school domains were tried, so nothing was
# visible from another session for a long stretch - indistinguishable from a
# hung process, and I twice concluded the run had died when it had not. A
# multi-hour job that someone else operates has to show progress.
COMMIT_EVERY = 5

# docs/SOURCES.md S2: fee structures change annually, so re-visiting a school
# sooner than this buys nothing.
REFRESH_DAYS = 180

# The whole extraction pipeline's version, DERIVED from the tier versions rather
# than written out by hand. `reextract` skips any document already read at this
# version, so bumping one extractor's `_v1` to `_v2` invalidates its markers on
# its own - there is no second place to remember to update, and no way to ship a
# parser change that silently never gets applied.
PIPELINE_VERSION = "|".join((tables.EXTRACTOR, ocr.EXTRACTOR, patterns.EXTRACTOR))

# Commit the markers periodically so a full pass is resumable. A national
# re-extract is hours of OCR; without this, interrupting it discarded all of it.
REEXTRACT_COMMIT_EVERY = 100


def _mark_extracted(session: Session, content_hash: str) -> None:
    """Record that the current pipeline has read these bytes.

    Called on EVERY outcome, not just a successful one. A document that yields
    nothing writes no observation, so `observations` alone cannot answer "have
    we read this?" - and the no-yield cases are the expensive ones: a scanned
    fee PDF that OCR fails on costs tens of seconds to fail again.

    Guarded by EXISTS rather than a bare INSERT: `extraction_runs.content_hash`
    FK-references `raw_documents`, and a small number of hashes are recorded on
    a `fetches` row with no matching `raw_documents` row - a gap from a crashed
    enrichment run, not something this stage can repair. Marking one would
    violate the FK and abort the whole reextract loop over one dangling row;
    skipping it just means that row gets retried (a no-op) next run too,
    which is cheap next to aborting thousands of valid documents.
    """
    session.execute(
        text(
            "INSERT INTO extraction_runs (content_hash, extractor)"
            " SELECT :h, :v WHERE EXISTS ("
            "   SELECT 1 FROM raw_documents WHERE content_hash = :h)"
            " ON CONFLICT DO NOTHING"
        ),
        {"h": content_hash, "v": PIPELINE_VERSION},
    )


@dataclass
class CoverageReport:
    """The per-hop numbers M2-1's DoD asks for."""

    considered: int = 0
    outcomes: Counter = field(default_factory=Counter)
    fee_found: int = 0
    fee_from_ocr: int = 0
    unreadable_fee: int = 0
    strength_found: int = 0
    contacts_found: int = 0
    student_data_blocked: int = 0
    review_raised: int = 0

    def as_dict(self) -> dict:
        return {
            "considered": self.considered,
            "outcomes": dict(self.outcomes),
            "fee_found": self.fee_found,
            "fee_from_ocr": self.fee_from_ocr,
            "unreadable_fee": self.unreadable_fee,
            "strength_found": self.strength_found,
            "contacts_found": self.contacts_found,
            "student_data_blocked": self.student_data_blocked,
            "review_raised": self.review_raised,
            "fee_coverage_pct": round(100 * self.fee_found / self.considered, 1)
            if self.considered
            else 0.0,
        }


# Board -> the institutions column holding that registry's stable ID. Lets a
# campaign enrich one registry at a time: the CISCE schools are a fresh 840 with
# no contacts at all, while the remaining CBSE ones are a long tail already
# half-worked, and mixing them makes the run's yield impossible to read.
BOARD_ID_COLUMNS = {"cbse": "cbse_affiliation_no", "cisce": "cisce_code"}


def targets(
    session: Session,
    limit: int | None,
    states: list[str] | None,
    per_state: int | None = None,
    redo_after_days: int | None = None,
    board: str | None = None,
) -> list[tuple]:
    """Institutions worth enriching: hard gate passed, has a website.

    Enrichment scope is campaign-driven (ADR-017); the national directory is
    already imported, but MPD work is 3-6 fetches each and follows priority.
    """
    clauses = ["has_class_12", "website IS NOT NULL", "status = 'active'"]
    # Government school networks are excluded from MPD enrichment. They fail the
    # affordability floor by construction (KV fees are a few thousand rupees a
    # year), so spending 3-6 fetches each buys nothing, and they dominate the
    # low affiliation numbers that ORDER BY id surfaces first.
    clauses.append(r"website !~* '(kvs\.ac\.in|navodaya|\.gov\.in|sainikschool)'")
    params: dict = {}
    if states:
        clauses.append("upper(state) = ANY(:states)")
        params["states"] = [s.upper() for s in states]
    # A stable ID is required because it forms the entity_key. Without one these
    # observations would carry a prefix the resolver has no stable-ID column
    # for, and every MPD fee would be silently orphaned.
    #
    # [M3-1] This used to read `cbse_affiliation_no IS NOT NULL`, which was
    # indistinguishable from "has a stable ID" while CBSE was the only registry.
    # It silently excluded all 1,928 CISCE schools from enrichment - and
    # enrichment is the ONLY route to a phone number or an email address, so
    # importing them bought a directory nobody could ring.
    if board:
        clauses.append(f"{BOARD_ID_COLUMNS[board]} IS NOT NULL")
    else:
        clauses.append("(cbse_affiliation_no IS NOT NULL OR cisce_code IS NOT NULL)")
    # RESUMABILITY. A school visited within the refresh window is skipped, so
    # re-running after an interruption continues where it stopped instead of
    # re-fetching an evening's work and hitting the same websites twice.
    # docs/SOURCES.md S2 sets that window at 180 days.
    window = redo_after_days if redo_after_days is not None else REFRESH_DAYS
    clauses.append(
        "(i.last_enriched_at IS NULL"
        f" OR i.last_enriched_at < now() - interval '{int(window)} days')"
    )

    where = " AND ".join(clauses).replace("i.last_enriched_at", "last_enriched_at")
    if per_state:
        # N per state, not N overall. Without this the whole budget goes to
        # whichever state has the lowest ids - Karnataka would consume a
        # national run entirely and the map would still show one state.
        sql = (
            "SELECT id, canonical_name, website, entity_key FROM ("
            "  SELECT id, canonical_name, website, coalesce('cbse:' || cbse_affiliation_no, 'cisce:' || cisce_code) AS entity_key,"
            "         row_number() OVER (PARTITION BY state ORDER BY id) AS rn"
            "    FROM institutions"
            f"   WHERE {where}"
            ") ranked WHERE rn <= :per_state ORDER BY id"
        )
        params["per_state"] = per_state
    else:
        sql = (
            "SELECT id, canonical_name, website, coalesce('cbse:' || cbse_affiliation_no, 'cisce:' || cisce_code) AS entity_key FROM institutions"
            f" WHERE {where} ORDER BY id"
        )
    if limit:
        sql += " LIMIT :limit"
        params["limit"] = limit
    return session.execute(text(sql), params).all()


def extract_contacts(body: bytes) -> MPDExtraction:
    """Phone and email from ANY school page, not just the MPD index.

    A school homepage or contact page publishes its phone and email in the
    header or footer far more reliably than the Appendix-IX proforma does - and
    449 homepages are already on disk, so reading them costs no fetches.
    Contacts are what the sales team actually needs, and phone coverage was 6%
    before this existed.

    Contacts ONLY. Never a fee: a number near the word "fee" on a homepage is
    not a fee, which is the whole lesson of M2-2.
    """
    if body[:5] == b"%PDF-":
        return MPDExtraction()
    tree = HTMLParser(body.decode("utf-8", "replace"))
    for tag in ("script", "style"):
        for node in tree.css(tag):
            node.decompose()
    page_text = tree.text(separator=" ")

    found: dict[str, object] = {}
    evidence: dict[str, str] = {}
    for field_name in ("email", "phone"):
        value = patterns.value_for(field_name, page_text)
        if value:
            found[field_name] = value
            evidence[field_name] = patterns.span_for(page_text, value)
    return MPDExtraction(**found, evidence=evidence)


def extract_document(
    body: bytes, *, is_fee_document: bool
) -> tuple[MPDExtraction, str]:
    """Tier 1 -> tier 1b (OCR) -> tier 2, in that order.

    Tier 2 never overwrites tier 1: a labelled value beats a pattern match,
    because the label is evidence of intent and the pattern is a guess about
    layout. OCR only runs when there is genuinely nothing to parse.
    """
    result = tables.extract(body, is_fee_document=is_fee_document)
    if result.student_data_detected:
        return result, tables.EXTRACTOR

    if result.unreadable_fee_document and is_fee_document:
        ocr_result = ocr.extract(body)
        if ocr_result.has_fee:
            return ocr_result, ocr.EXTRACTOR
        return result, tables.EXTRACTOR

    if body[:5] == b"%PDF-":
        _, page_text = tables.pdf_rows(body)
    else:
        _, page_text = tables.html_rows(body.decode("utf-8", "replace"))
    filled = patterns.fill_gaps(result, page_text)
    # The tier that produced the value is recorded, never inferred from how the
    # value looks. ADR-015 compares extractor versions over identical documents,
    # so a wrong label here makes the eval meaningless.
    tier = (
        patterns.EXTRACTOR
        if set(filled.evidence) - set(result.evidence)
        else tables.EXTRACTOR
    )
    return filled, tier


def claims_from(result: MPDExtraction) -> list[Claim]:
    """Map an extraction onto the canonical field vocabulary."""
    mapping = {
        "fee_annual_inr_max": "fee_annual_inr",
        "fee_year": "fee_year",
        "class_12_total": "class_12_total",
        "pcm_12_count": "pcm_12_count",
        "total_enrollment": "total_enrollment",
        "total_teachers": "total_teachers",
        "pincode": "pincode",
        "email": "email",
        "phone": "phone",
        "principal_name": "principal_name",
        "counsellor_name": "counsellor_name",
    }
    claims = []
    for attribute, field_name in mapping.items():
        value = getattr(result, attribute)
        if value is None:
            continue
        claims.append(
            Claim(
                field=field_name,
                value=str(value),
                evidence=result.evidence.get(attribute, ""),
            )
        )
    if result.streams:
        claims.append(
            Claim(
                field="streams",
                value=",".join(result.streams),
                evidence=result.evidence.get("streams", ""),
            )
        )
    return claims


def _raise_review(
    session: Session, institution_id: int, kind: str, payload: dict
) -> None:
    import json

    session.execute(
        text("INSERT INTO review_queue (kind, payload) VALUES (:k, cast(:p as jsonb))"),
        {"k": kind, "p": json.dumps({"institution_id": institution_id, **payload})},
    )


async def discover_and_extract(
    fetcher,
    session: Session,
    *,
    limit: int | None = None,
    states: list[str] | None = None,
    observed_at: datetime | None = None,
    per_state: int | None = None,
    redo_after_days: int | None = None,
    board: str | None = None,
) -> CoverageReport:
    report = CoverageReport()
    rows = targets(session, limit, states, per_state, redo_after_days, board)
    report.considered = len(rows)

    log.info("enriching %d schools", len(rows))
    for index, (institution_id, name, website, entity_key) in enumerate(rows, 1):
        # A full run is tens of minutes. Committing periodically keeps it out of
        # one enormous transaction, so an interruption keeps the work done so far
        # rather than discarding all of it.
        if index % COMMIT_EVERY == 0:
            session.commit()
            log.info(
                "%d/%d processed, %d fees found", index, len(rows), report.fee_found
            )
        try:
            found = await cbse_mpd.discover(
                fetcher,
                session,
                institution_id=institution_id,
                canonical_name=name,
                website=website,
            )
        except Exception as exc:  # noqa: BLE001 - one bad site must not stop the run
            log.info("%s: discovery failed: %s", name, exc)
            report.outcomes["error"] += 1
            continue

        report.outcomes[found.outcome] += 1
        # Stamped whatever the outcome. A school whose website is dead is DONE -
        # re-trying it every run would spend the whole budget on the same
        # unreachable sites.
        session.execute(
            text("UPDATE institutions SET last_enriched_at = :at WHERE id = :id"),
            {"at": observed_at or datetime.now().astimezone(), "id": institution_id},
        )

        got_contacts = False
        for content_hash, is_fee in (
            (found.mpd_hash, False),
            (found.fee_hash, True),
        ):
            if not content_hash:
                continue
            if is_fee and content_hash == found.mpd_hash:
                continue  # one document served both roles; already handled
            body = load(session, content_hash)
            if body is None:
                continue

            result, extractor = extract_document(body, is_fee_document=is_fee)

            if result.student_data_detected:
                # Delete the document, learn the URL, emit nothing.
                from school_intel.fetch import denylist

                url = found.fee_url if is_fee else found.mpd_url
                report.student_data_blocked += 1
                if url:
                    denylist.learn(session, url, "student_data_detected", "extractor")
                delete_document(session, content_hash)
                continue

            if result.unreadable_fee_document:
                report.unreadable_fee += 1
                _raise_review(
                    session,
                    institution_id,
                    "unreadable_fee_document",
                    {
                        "url": found.fee_url,
                        "reason": "no text layer; OCR yielded no fee",
                    },
                )
                continue

            confidence = (
                ocr.observation_confidence() if extractor == ocr.EXTRACTOR else 1.0
            )

            claims = claims_from(result)
            if not claims:
                continue
            write_observations(
                session,
                claims=claims,
                entity_key=entity_key,
                source_id=SOURCE_ID,
                extractor=extractor,
                observed_at=observed_at or datetime.now().astimezone(),
                content_hash=content_hash,
                confidence=confidence,
            )

            if result.has_fee:
                report.fee_found += 1
                if extractor == ocr.EXTRACTOR:
                    report.fee_from_ocr += 1
                reason = (
                    ocr.needs_review(result) if extractor == ocr.EXTRACTOR else None
                )
                if reason:
                    report.review_raised += 1
                    _raise_review(
                        session,
                        institution_id,
                        "ocr_fee_unverified",
                        {"reason": reason},
                    )
            if result.class_12_total is not None:
                report.strength_found += 1
            if result.email or result.phone:
                report.contacts_found += 1
                got_contacts = True

        # The homepage, mined for contacts only. Most schools return
        # `not_found` - no MPD page - and before this the live run discarded a
        # homepage it had already fetched, along with the phone and email in its
        # footer. Attribution here is by institution_id, so unlike `reextract`
        # it is exact and needs no unique-URL guard.
        if not got_contacts and found.home_hash:
            body = load(session, found.home_hash)
            contacts = extract_contacts(body) if body else MPDExtraction()
            claims = claims_from(contacts)
            if claims:
                write_observations(
                    session,
                    claims=claims,
                    entity_key=entity_key,
                    source_id=SOURCE_ID,
                    extractor=patterns.EXTRACTOR,
                    observed_at=observed_at or datetime.now().astimezone(),
                    content_hash=found.home_hash,
                    confidence=1.0,
                )
                report.contacts_found += 1

    return report


def reextract(
    session: Session, observed_at: datetime | None = None, *, full: bool = False
) -> CoverageReport:
    """Re-run the parsers over stored MPD documents. ZERO fetches.

    This is the ADR-009 payoff for S2, and it was missing: improving the fee
    parser meant re-crawling 449 school websites, which is both slow and rude.
    Now it is a minutes-long loop over bytes already on disk.

    Documents are mapped to institutions by matching the fetch URL against
    `institutions.website`, and a PDF is treated as a fee document while HTML is
    treated as an index. That distinction only controls whether contacts are
    extracted, so getting it wrong costs a contact rather than a wrong fee.

    UNIQUE MATCHES ONLY. `fetches` carries no institution_id, so that URL match
    is the only link back - and it is not one-to-one. 997 institutions share
    `nadunedu.se.ap.gov.in`, 128 share `narayanaschools.in`, and 117 have their
    website recorded as `gmail.com`; a substring match hands one school's
    document to every one of them. That both multiplied this loop 20x (52,174
    pairs over 2,538 documents) and wrote one school's fee and phone as
    observations for 127 others. A document matching more than one institution
    is therefore skipped rather than guessed at - hard rule 6, missing beats
    wrong. Chains that genuinely share a website lose their re-extraction here;
    they keep whatever the enrich pass recorded, which knew the institution.
    """
    report = CoverageReport()
    # Incremental by default: a document already read at this pipeline version
    # is skipped. `full` ignores the markers and re-reads everything.
    marker_filter = (
        ""
        if full
        else (
            " AND NOT EXISTS (SELECT 1 FROM extraction_runs er"
            "  WHERE er.content_hash = counted.content_hash AND er.extractor = :v)"
        )
    )
    rows = session.execute(
        text(
            "SELECT content_hash, url, id, entity_key FROM ("
            "  SELECT pairs.*, count(*) OVER (PARTITION BY content_hash) AS matches"
            "    FROM ("
            "      SELECT DISTINCT ON (f.content_hash, i.id)"
            "             f.content_hash, f.url, i.id,"
            "             coalesce('cbse:' || i.cbse_affiliation_no,"
            "                      'cisce:' || i.cisce_code) AS entity_key"
            "        FROM fetches f"
            "        JOIN institutions i"
            "          ON i.website IS NOT NULL"
            "         AND f.url ILIKE '%' || i.website || '%'"
            "       WHERE f.source_id = :src AND f.content_hash IS NOT NULL"
            "         AND (i.cbse_affiliation_no IS NOT NULL"
            "              OR i.cisce_code IS NOT NULL)"
            "       ORDER BY f.content_hash, i.id, f.id"
            "    ) pairs"
            ") counted WHERE matches = 1" + marker_filter + " ORDER BY content_hash"
        ),
        {"src": SOURCE_ID, "v": PIPELINE_VERSION},
    ).all()
    report.considered = len({row[2] for row in rows})

    # OCR on a scanned fee PDF takes tens of seconds, so this loop runs for
    # nearly an hour with nothing on stdout. Log like enrich does - a silent
    # stage is indistinguishable from a hung one.
    log.info("re-extracting %d stored documents", len(rows))
    for position, (content_hash, url, institution_id, entity_key) in enumerate(rows, 1):
        if position % REEXTRACT_COMMIT_EVERY == 0:
            session.commit()
            log.info(
                "%d/%d documents, %d fees found", position, len(rows), report.fee_found
            )
        body = load(session, content_hash)
        if body is None:
            # Missing or unreadable bytes - quarantined by an antivirus, say.
            # Marked, so we stop retrying it on every run.
            _mark_extracted(session, content_hash)
            continue
        is_fee = body[:5] == b"%PDF-"
        result, extractor = extract_document(body, is_fee_document=is_fee)

        if result.student_data_detected:
            from school_intel.fetch import denylist

            report.student_data_blocked += 1
            denylist.learn(session, url, "student_data_detected", "extractor")
            # No marker: the document is deleted, and the CASCADE would drop the
            # row anyway. It can never be read again.
            delete_document(session, content_hash)
            continue
        if result.unreadable_fee_document:
            report.unreadable_fee += 1
            # THE expensive case. OCR spent tens of seconds and got nothing;
            # doing that again next run is the waste this table exists for.
            _mark_extracted(session, content_hash)
            continue

        claims = claims_from(result)
        if not claims:
            # Not an MPD page - but a homepage still carries a phone and an
            # email in its header or footer, which is what the sales team needs
            # most. We already store 449 homepages, so this costs no fetches.
            contacts = extract_contacts(body)
            claims = claims_from(contacts)
            if not claims:
                _mark_extracted(session, content_hash)
                continue
            result, extractor = contacts, patterns.EXTRACTOR
        confidence = ocr.observation_confidence() if extractor == ocr.EXTRACTOR else 1.0
        write_observations(
            session,
            claims=claims,
            entity_key=entity_key,
            source_id=SOURCE_ID,
            extractor=extractor,
            observed_at=observed_at or datetime.now().astimezone(),
            content_hash=content_hash,
            confidence=confidence,
        )
        if result.has_fee:
            report.fee_found += 1
            if extractor == ocr.EXTRACTOR:
                report.fee_from_ocr += 1
        if result.class_12_total is not None:
            report.strength_found += 1
        if result.email or result.phone:
            report.contacts_found += 1
        _mark_extracted(session, content_hash)
    return report


def backfill_last_enriched(session: Session) -> int:
    """Derive `last_enriched_at` from the fetch log.

    Needed because enrichment runs that predate that column left no stamp, and
    without one a resumed run would redo them. Matching a fetch URL against the
    school's website is slower than the stamp, but it is a one-off repair rather
    than the normal path.

    Safe to re-run: it only ever moves the timestamp forward.

    UNIQUE MATCHES ONLY, for the same reason `reextract` needs the guard: a
    fetch URL matches every institution whose website is a substring of it, and
    117 schools have `gmail.com` recorded as their website. Marking those as
    visited on the strength of someone else's fetch would skip them for the next
    180 days without anyone ever having looked at their site.
    """
    result = session.execute(
        text(
            "UPDATE institutions i SET last_enriched_at = sub.latest"
            "  FROM ("
            "    SELECT i2.id, max(f.requested_at) AS latest"
            "      FROM institutions i2"
            "      JOIN fetches f"
            "        ON f.source_id = :src"
            "       AND i2.website IS NOT NULL"
            # Scoped to unstamped rows only - this is a repair for the crash
            # window (a batch fetched but not yet committed to
            # last_enriched_at), not a re-check of schools the direct stamp
            # already covered. Without this the ILIKE join below - which
            # cannot use an index - re-scanned all of `fetches` for every one
            # of 33k institutions on every call, including the ~16k already
            # stamped: 20+ minutes and growing every enrichment run.
            "       AND i2.last_enriched_at IS NULL"
            "       AND f.url ILIKE '%' || i2.website || '%'"
            "     WHERE NOT EXISTS ("
            "       SELECT 1 FROM institutions other"
            "        WHERE other.website IS NOT NULL"
            "          AND other.id <> i2.id"
            "          AND other.website = i2.website)"
            "     GROUP BY i2.id"
            "  ) sub"
            " WHERE i.id = sub.id"
            "   AND (i.last_enriched_at IS NULL OR i.last_enriched_at < sub.latest)"
        ),
        {"src": SOURCE_ID},
    )
    return result.rowcount or 0
