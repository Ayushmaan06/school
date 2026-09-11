"""S1 - CBSE SARAS importer. The backbone of the system (ADR-001).

[VERIFIED M0-0] The affiliated list is a POST, not a GET:

    GET  /SARAS/AffiliatedList/ListOfSchdirReportNew?ID1=D  -> the CLOSED list only
    POST /SARAS/AffiliatedList/ListOfSchdirReport           -> the affiliated list

The POST needs an `__RequestVerificationToken` harvested from a prior GET, sent
back with the `.AspNetCore.Antiforgery.*` cookie from that same GET. So this
importer holds a session rather than making stateless fetches.

Scope (ADR-017): import is national, enrichment is campaign-scoped. The list is
~38 POSTs and already carries principal, address and website, so a national
directory is nearly free. The expensive stage is MPD enrichment (M2-1).
"""

import logging
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from school_intel.extract.parsers.cbse_saras import (
    SENIOR_SECONDARY,
    ListPage,
    parse_list,
)
from school_intel.fetch.client import Fetcher
from school_intel.fetch.store import load
from school_intel.jobs import queue
from school_intel.sources.base import (
    ImportResult,
    RowCountMismatchError,
    StructureDriftError,
)

log = logging.getLogger(__name__)

SOURCE_ID = "cbse_saras"
BASE = "https://saras.cbse.gov.in/SARAS/AffiliatedList/"
LIST_SEED_URL = f"{BASE}ListOfSchdirReportNew?ID1=D"
LIST_POST_URL = f"{BASE}ListOfSchdirReport"
DETAIL_URL = f"{BASE}AfflicationDetails/{{affiliation_no}}"

# Fraction of the previous snapshot below which a run aborts. Hard rule 10.
DRIFT_FLOOR = 0.5

# [VERIFIED M0-0] SARAS state codes are numeric, not names. Lifted verbatim
# from the live form's <select id="State"> rather than guessed - an earlier
# hand-written version of this table gave Tamil Nadu and Odisha the same code.
STATE_CODES: dict[str, str] = {
    "ANDAMAN & NICOBAR": "25",
    "ANDHRA PRADESH": "1",
    "ARUNACHAL PRADESH": "22",
    "ASSAM": "2",
    "BIHAR": "3",
    "CHANDIGARH": "26",
    "CHATTISGARH": "33",
    "DADAR & NAGAR HAVELI": "30",
    "DAMAN & DIU": "31",
    "DELHI": "27",
    "FOREIGN SCHOOLS": "50",
    "GOA": "28",
    "GUJARAT": "4",
    "HARYANA": "5",
    "HIMACHAL PRADESH": "6",
    "JAMMU & KASHMIR": "7",
    "JHARKHAND": "34",
    "KARNATAKA": "8",
    "KERALA": "9",
    "LADAKH": "37",
    "LAKSHADWEEP": "32",
    "MADHYA PRADESH": "10",
    "MAHARASHTRA": "11",
    "MANIPUR": "12",
    "MEGHALAYA": "13",
    "MIZORAM": "23",
    "NAGALAND": "14",
    "ODISHA": "15",
    "PUDUCHERRY": "29",
    "PUNJAB": "16",
    "RAJASTHAN": "17",
    "SIKKIM": "18",
    "TAMILNADU": "19",
    "TELANGANA": "36",
    "TRIPURA": "20",
    "UTTAR PRADESH": "21",
    "UTTARAKHAND": "35",
    "WEST BENGAL": "24",
}

# v1 enrichment priority (ADR-017). Import is national; these are the
# geographies whose institutions get the expensive MPD pass first.
ENRICHMENT_FIRST = ("KARNATAKA", "TAMILNADU", "MAHARASHTRA", "TELANGANA")


_TOKEN = re.compile(
    r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', re.IGNORECASE
)


# [VERIFIED M0-0] SchoolStatusWise option values, read from the live form.
# "0" is the "Select" placeholder, which the server treats as "no filter".
SCHOOL_STATUS_ALL = "0"
SCHOOL_STATUS_MIDDLE = "1"
SCHOOL_STATUS_SECONDARY = "2"
SCHOOL_STATUS_SENIOR_SECONDARY = "3"


def extract_token(html: str) -> str | None:
    m = _TOKEN.search(html)
    return m.group(1) if m else None


def build_state_form(
    token: str, state_code: str, senior_only: bool = False
) -> dict[str, str]:
    """The POST body SARAS's model binder accepts.

    `SchoolStatusWise` is a SERVER-side filter, so the hard gate can be a request
    parameter rather than a post-parse step. We still parse and count every level
    when senior_only is False, because the level distribution is what M4-3 diffs.
    """
    return {
        "__RequestVerificationToken": token,
        "MainRadioValue": "State_wise",
        "State": state_code,
        "District": "0",
        "Region": "",
        "SchoolStatusWise": (
            SCHOOL_STATUS_SENIOR_SECONDARY if senior_only else SCHOOL_STATUS_ALL
        ),
        "InstName_orAddress": "",
        "RegiAffNo": "0",
        "__Invariant": "RegiAffNo",
    }


def check_drift(session: Session, rows_parsed: int, scope: str) -> None:
    """Hard rule 10. Abort WITHOUT writing a snapshot if the count collapsed.

    Compares LIKE WITH LIKE. [VERIFIED M5] a national snapshot (33,146 rows)
    followed by a single-state run (2,252 rows) tripped the guard at "7%" and
    aborted - correct arithmetic, wrong comparison. A one-state run is not a
    93% collapse of the country. The snapshot's scope is recorded in the
    content_hash-adjacent scope tag, and only snapshots of the same scope are
    compared.
    """
    previous = session.execute(
        text(
            "SELECT row_count FROM registry_snapshots WHERE source_id = :s"
            "   AND coalesce(scope, '') = :scope"
            " ORDER BY taken_at DESC LIMIT 1"
        ),
        {"s": SOURCE_ID, "scope": scope},
    ).scalar()
    if previous and rows_parsed < previous * DRIFT_FLOOR:
        raise StructureDriftError(
            f"{scope}: parsed {rows_parsed} rows, previous snapshot had {previous} "
            f"({rows_parsed / previous:.0%}). Aborting without writing a snapshot - "
            "this is almost certainly a parser or source change, not mass closure."
        )


def check_row_count(page: ListPage, scope: str) -> None:
    """Fail loudly when our count disagrees with the page's own stated total."""
    if page.stated_total is None:
        log.warning("%s: page states no total; cannot verify the row count", scope)
        return
    if page.seen != page.stated_total:
        raise RowCountMismatchError(
            f"{scope}: parsed {page.seen} rows ({len(page.rows)} usable, "
            f"{len(page.skipped)} skipped) but the page states {page.stated_total}"
        )


def enqueue_detail_fetches(session: Session, page: ListPage) -> int:
    """One detail job per senior-secondary school, deduped on its entity key.

    Only Senior Secondary rows are enqueued. [VERIFIED M0-0] that is ~29% of the
    list, so this filter is the single largest cost saving in the pipeline and it
    happens BEFORE any detail or site fetch (ADR-007).
    """
    enqueued = 0
    for row in page.rows:
        if row.status != SENIOR_SECONDARY:
            continue
        job_id = queue.enqueue(
            session,
            kind="fetch_saras_detail",
            payload={
                "url": DETAIL_URL.format(affiliation_no=row.affiliation_no),
                "entity_key": row.entity_key,
                "source_id": SOURCE_ID,
            },
            dedupe_key=f"saras_detail:{row.affiliation_no}",
        )
        if job_id is not None:
            enqueued += 1
    return enqueued


def write_snapshot(
    session: Session, content_hash: str, row_count: int, scope: str = ""
) -> int:
    """`scope` records WHICH states this snapshot covers, so the drift guard and
    M4-3's signal diffing only ever compare runs of the same shape."""
    return session.execute(
        text(
            "INSERT INTO registry_snapshots (source_id, row_count, content_hash,"
            " scope) VALUES (:s, :n, :h, :scope) RETURNING id"
        ),
        {"s": SOURCE_ID, "n": row_count, "h": content_hash, "scope": scope},
    ).scalar_one()


class SarasImporter:
    """Holds the antiforgery session for the run."""

    source_id = SOURCE_ID

    def __init__(
        self, fetcher: Fetcher, session: Session, states: list[str] | None = None
    ):
        self.fetcher = fetcher
        self.session = session
        self.states = states or list(STATE_CODES)
        self._token: str | None = None

    async def _ensure_token(self) -> str:
        if self._token is not None:
            return self._token
        result = await self.fetcher.fetch(self.session, LIST_SEED_URL, SOURCE_ID)
        if result.content_hash is None:
            raise RuntimeError(
                f"could not load the SARAS seed page (status={result.status})"
            )
        html = (load(self.session, result.content_hash) or b"").decode(
            "utf-8", "replace"
        )
        token = extract_token(html)
        if token is None:
            raise RuntimeError(
                "no __RequestVerificationToken on the SARAS seed page - the form "
                "changed; re-verify docs/SOURCES.md S1 before touching this parser"
            )
        self._token = token
        return token

    async def fetch_state(self, state: str) -> tuple[ListPage, str]:
        code = STATE_CODES[state]
        token = await self._ensure_token()
        result = await self.fetcher.fetch(
            self.session,
            LIST_POST_URL,
            SOURCE_ID,
            method="POST",
            data=build_state_form(token, code),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": LIST_SEED_URL,
            },
        )
        if result.content_hash is None:
            raise RuntimeError(f"{state}: POST failed (status={result.status})")
        html = (load(self.session, result.content_hash) or b"").decode(
            "utf-8", "replace"
        )
        return parse_list(html), result.content_hash

    async def run(self) -> ImportResult:
        total_rows = 0
        total_jobs = 0
        per_state: dict[str, int] = {}
        last_hash: str | None = None

        for state in self.states:
            page, content_hash = await self.fetch_state(state)
            check_row_count(page, f"cbse_saras/{state}")
            total_rows += len(page.rows)
            total_jobs += enqueue_detail_fetches(self.session, page)
            per_state[state] = len(page.rows)
            last_hash = content_hash
            log.info(
                "%s: %d rows, %d senior-secondary",
                state,
                len(page.rows),
                sum(1 for r in page.rows if r.has_class_12),
            )

        # The scope tag is the sorted state list, so a national run and a
        # single-state run are never compared against each other.
        scope = ",".join(sorted(self.states))
        check_drift(self.session, total_rows, scope)
        snapshot_id = (
            write_snapshot(self.session, last_hash, total_rows, scope)
            if last_hash
            else None
        )
        return ImportResult(
            snapshot_id=snapshot_id,
            rows_seen=total_rows,
            jobs_enqueued=total_jobs,
            notes={"per_state": per_state, "states": len(self.states)},
        )
