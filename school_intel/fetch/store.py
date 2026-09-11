"""Content-addressed raw document storage.

Re-fetching an unchanged page writes a `fetches` row (proving freshness) but no
new document. That dedup is also what makes `cli extract --reextract` free: the
bytes are already here, so improving a parser is a minutes-long loop rather than
a re-crawl (ADR-009).
"""

import hashlib
import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def content_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def storage_path(root: Path, digest: str, media_type: str) -> Path:
    """raw/9f/2a/9f2a...c1 - two levels of fan-out keeps directories small."""
    suffix = ".pdf" if "pdf" in media_type.lower() else ".bin"
    return root / digest[:2] / digest[2:4] / f"{digest}{suffix}"


def store(
    session: Session, root: Path, body: bytes, media_type: str
) -> tuple[str, bool]:
    """Write the document if its hash is new. Returns (hash, was_new)."""
    digest = content_hash(body)
    existing = session.execute(
        text("SELECT 1 FROM raw_documents WHERE content_hash = :h"), {"h": digest}
    ).first()
    if existing:
        return digest, False

    path = storage_path(root, digest, media_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    session.execute(
        text(
            "INSERT INTO raw_documents (content_hash, storage_path, media_type, bytes) "
            "VALUES (:h, :p, :m, :b) ON CONFLICT (content_hash) DO NOTHING"
        ),
        {"h": digest, "p": str(path), "m": media_type, "b": len(body)},
    )
    return digest, True


def load(session: Session, digest: str) -> bytes | None:
    """Read stored bytes back. Used by `extract --reextract`, which makes no fetches."""
    row = session.execute(
        text("SELECT storage_path FROM raw_documents WHERE content_hash = :h"),
        {"h": digest},
    ).first()
    if row is None:
        return None
    path = Path(row[0])
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        # A stored document that exists but will not open. Seen for real: we
        # crawl ~33,000 school websites and some of them are compromised, so
        # the antivirus quarantines the bytes we saved and the file then fails
        # to open with EINVAL. Returning None is the same answer callers
        # already handle for a missing file, and one poisoned page must not
        # abort a multi-hour national pass - the per-school try/except upstream
        # only wraps discovery, not extraction.
        #
        # We never execute these bytes; extraction parses HTML and never runs
        # script. Losing the document is the right outcome anyway: a fee or a
        # phone number scraped off an injected page is not evidence of
        # anything, and hard rule 6 says missing beats wrong.
        log.warning("stored document %s is unreadable (%s); skipping", digest, exc)
        return None


def delete_document(session: Session, digest: str) -> None:
    """Remove a document found to contain student data (COMPLIANCE.md).

    This is the ONE deletion the system performs, and it exists to satisfy the
    PII boundary rather than to save space. It does not touch `fetches` or
    `observations` - the record that we fetched and then discarded is itself
    auditable evidence, which is what hard rule 4 protects.
    """
    row = session.execute(
        text("SELECT storage_path FROM raw_documents WHERE content_hash = :h"),
        {"h": digest},
    ).first()
    if row is not None:
        Path(row[0]).unlink(missing_ok=True)
    session.execute(
        text("DELETE FROM raw_documents WHERE content_hash = :h"), {"h": digest}
    )
