"""Fill a placeholder student count from the DB-wide distribution, not the school.

Unlike `guess_email.py`, this has NO per-school evidence at all: it is a random
integer between the mean and median of every `total_enrollment` we do have,
handed to schools we have enriched and can reach but never got a real number
from. It is written to `institutions.student_count_guessed`, never to
`total_enrollment`, so `resolve`, `score` and `pcm_12_count` stay based on
published (or teacher-count-derived) facts only (hard rule 6). The UI labels
it "unverified source" and it must never be read by anything that scores.
"""

from __future__ import annotations

import random
import statistics
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from school_intel.extract.mpd.run import BOARD_ID_COLUMNS


def population_stats(session: Session) -> tuple[int, int] | None:
    """Mean and median (rounded) of every real `total_enrollment` we hold."""
    values = session.execute(
        text(
            "SELECT total_enrollment FROM institutions"
            " WHERE status = 'active' AND total_enrollment IS NOT NULL"
        )
    ).scalars().all()
    if not values:
        return None
    return round(statistics.mean(values)), round(statistics.median(values))


def backfill(session: Session, *, board: str | None = None) -> dict[str, int | str]:
    """Fill `student_count_guessed` for enriched, contactable schools with no count.

    Eligible: enriched (`last_enriched_at` set), contactable by phone, email or
    a guessed email, and missing a real `total_enrollment`. Safe to re-run; it
    overwrites the previous guess with a fresh random draw.
    """
    stats = population_stats(session)
    if stats is None:
        return {"considered": 0, "guessed": 0, "reason": "no_student_count_data"}
    mean, median = stats
    low, high = sorted((mean, median))

    where = [
        "i.status = 'active'",
        "i.total_enrollment IS NULL",
        "i.last_enriched_at IS NOT NULL",
        "(i.phone IS NOT NULL OR i.email IS NOT NULL OR i.email_guessed IS NOT NULL)",
    ]
    if board is not None:
        where.append(f"i.{BOARD_ID_COLUMNS[board]} IS NOT NULL")
    ids = session.execute(
        text(f"SELECT i.id FROM institutions i WHERE {' AND '.join(where)}")
    ).scalars().all()

    now = datetime.now(UTC)
    for inst_id in ids:
        value = low if low == high else random.randint(low, high)
        session.execute(
            text(
                "UPDATE institutions SET student_count_guessed = :v,"
                " student_count_guessed_at = :t WHERE id = :id"
            ),
            {"v": value, "t": now, "id": inst_id},
        )

    return {
        "considered": len(ids),
        "guessed": len(ids),
        "population_mean": mean,
        "population_median": median,
    }
