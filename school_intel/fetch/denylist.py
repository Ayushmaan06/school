"""HARD GATE - ADR-006 / DPDP Act 2023 s.9. Do not weaken.

Blocking happens at fetch time, not extraction time, not storage time. A blocked
URL is never requested, so the bytes never exist on our disk.

Do not add an override parameter, an environment variable, or a "temporary"
bypass. This is hard rule 1.
"""

import re
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.orm import Session

# Matched case-insensitively against the FULL url (path and query).
URL_DENY_PATTERNS = [
    r"result",
    r"results",
    r"merit",
    r"topper",
    r"toppers",
    r"marks",
    r"marksheet",
    r"scorecard",
    r"rank[-_]?list",
    r"admission[-_]?list",
    r"selected[-_]?candidate",
    r"student[-_]?list",
    r"studentlist",
    r"class[-_]?x{1,2}i{0,2}[-_]?result",
    r"board[-_]?result",
    r"cbse[-_]?result",
    r"award",
    r"prize[-_]?list",
    r"scholarship[-_]?list",
    r"attendance",
    r"admit[-_]?card",
    r"hall[-_]?ticket",
    r"transfer[-_]?certificate",
    r"tc[-_]?issued",
]

_COMPILED = [(p, re.compile(p, re.IGNORECASE)) for p in URL_DENY_PATTERNS]


@lru_cache(maxsize=4096)
def match_static(url: str) -> str | None:
    """Return the deny pattern this URL trips, or None.

    URL-ONLY. Never run these patterns over page text: the compliant CBSE
    Appendix-IX proforma titles a section "C: RESULT AND ACADEMICS", and the
    fee-structure link lives inside it. A text-level rule would blocklist
    essentially every Mandatory Disclosure page in India - deleting the only
    source of fee data in the system while appearing to work. [VERIFIED M0-0]
    """
    for pattern, rx in _COMPILED:
        if rx.search(url):
            return pattern
    return None


def is_learned(session: Session, url: str) -> bool:
    """URLs found at extraction time to contain student PII (COMPLIANCE.md)."""
    return (
        session.execute(
            text("SELECT 1 FROM denylist_learned WHERE url = :url"), {"url": url}
        ).first()
        is not None
    )


def blocked_reason(url: str, session: Session | None = None) -> str | None:
    """The single entrypoint. Non-None means: do not make the request."""
    static = match_static(url)
    if static is not None:
        return f"pattern:{static}"
    if session is not None and is_learned(session, url):
        return "learned:student_data_detected"
    return None


def learn(session: Session, url: str, reason: str, added_by: str) -> None:
    """Record a URL discovered to hold student data so it is never fetched again."""
    session.execute(
        text(
            "INSERT INTO denylist_learned (url, reason, added_by) "
            "VALUES (:url, :reason, :by) ON CONFLICT (url) DO NOTHING"
        ),
        {"url": url, "reason": reason, "by": added_by},
    )
