"""Per-field source priority. Implements the table in docs/SOURCES.md.

"First source in the list that has a live observation wins. Ties within one
source: newer `observed_at` wins. **The loser is never deleted**" - it stays in
`observations` and is shown by `/institutions/{id}/why`.

This is `Project-Doc.md` 6.11 implemented literally, including its correct
carve-out that a school's own site beats a stale government dataset for
leadership but never for board status.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Order matters: index 0 wins. A field absent from this table has no policy, and
# resolve() will refuse to guess one.
FIELD_PRIORITY: dict[str, tuple[str, ...]] = {
    "name": ("cbse_saras", "cisce", "ib", "cambridge", "udise", "cbse_mpd"),
    "address": ("cbse_saras", "udise", "cbse_mpd"),
    "pincode": ("cbse_saras", "udise", "cbse_mpd"),
    "city": ("pin_centroids", "cbse_saras", "udise"),
    "district": ("pin_centroids", "cbse_saras", "udise"),
    "state": ("pin_centroids", "cbse_saras", "udise"),
    "metro_area": ("pin_centroids",),
    # An institution's own site NEVER wins on board status - only the board that
    # awarded the affiliation can say whether it is current.
    "board": ("cbse_saras", "cisce", "ib", "cambridge"),
    "board_status": ("cbse_saras", "cisce", "ib", "cambridge"),
    "board_valid_from": ("cbse_saras", "cisce", "ib", "cambridge"),
    "board_valid_to": ("cbse_saras", "cisce", "ib", "cambridge"),
    "has_class_12": ("cbse_saras", "udise", "cbse_mpd"),
    "grade_low": ("cbse_saras", "udise", "cbse_mpd"),
    "grade_high": ("cbse_saras", "udise", "cbse_mpd"),
    "streams": ("cbse_mpd", "cambridge", "ib", "cbse_saras"),
    # No other source has fee data at all (docs/SOURCES.md S2).
    "fee_annual_inr": ("cbse_mpd",),
    "fee_year": ("cbse_mpd",),
    "fee_component_breakdown": ("cbse_mpd",),
    "class_12_total": ("cbse_mpd", "udise"),
    "pcm_12_count": ("cbse_mpd", "udise"),
    "pcm_12_count_year": ("cbse_mpd", "udise"),
    "total_enrollment": ("udise", "cbse_mpd"),
    # Only the school's own disclosure publishes staff counts.
    "total_teachers": ("cbse_mpd",),
    "enrollment_year": ("udise", "cbse_mpd"),
    "medium": ("udise", "cbse_saras"),
    "management_type": ("udise", "cbse_saras"),
    "legal_entity_name": ("cbse_saras", "cbse_mpd"),
    # The school's own site is fresher for leadership (6.8, 6.11).
    "principal_name": ("cbse_mpd", "cbse_saras", "udise"),
    "principal_title": ("cbse_mpd", "cbse_saras"),
    "counsellor_name": ("cbse_mpd",),
    "counsellor_title": ("cbse_mpd",),
    "website": ("cbse_mpd", "cbse_saras", "serper"),
    "email": ("cbse_mpd", "cbse_saras"),
    "phone": ("cbse_mpd", "cbse_saras"),
    "year_founded": ("cbse_saras", "udise"),
    "status": ("cbse_saras", "udise"),
    "institution_type": ("cbse_saras", "cisce", "ib", "cambridge", "udise"),
    "gender": ("cbse_saras", "udise"),
    "residential": ("cbse_saras", "udise"),
    "group_name": ("group_directory",),
    "group_campus_count": ("group_directory",),
}

# Numeric fields where a disagreement between sources is itself signal
# (Project-Doc 6.7): store both, surface both, raise a review_queue conflict.
DISPUTE_THRESHOLD = 0.20
DISPUTED_FLAGS: dict[str, str] = {
    "fee_annual_inr": "fee_disputed",
    "total_enrollment": "enrollment_disputed",
    "class_12_total": "enrollment_disputed",
    "pcm_12_count": "enrollment_disputed",
}


class NoPolicyError(KeyError):
    """A field with no entry in FIELD_PRIORITY.

    Refused rather than defaulted. Silently falling back to authority_tier would
    make a policy decision invisibly, which is exactly what 6.11 rejected.
    """


@dataclass(frozen=True, slots=True)
class Candidate:
    """One source's claim about one field."""

    observation_id: int
    source_id: str
    value: str
    observed_at: datetime
    evidence_span: str | None = None


@dataclass(frozen=True, slots=True)
class Resolution:
    winner: Candidate | None
    losers: tuple[tuple[Candidate, str], ...]  # (candidate, lost_because)
    disputed: bool = False


def resolve(field: str, candidates: list[Candidate]) -> Resolution:
    """Pick the winning claim and record why each loser lost.

    `lost_because` is a human-readable reason, because it is rendered verbatim
    by /institutions/{id}/why.
    """
    if field not in FIELD_PRIORITY:
        raise NoPolicyError(
            f"no conflict policy for field {field!r}. Add it to FIELD_PRIORITY "
            "and to the table in docs/SOURCES.md in the same change."
        )
    if not candidates:
        return Resolution(winner=None, losers=())

    order = FIELD_PRIORITY[field]
    ranked = [c for c in candidates if c.source_id in order]
    ignored = [
        (c, f"{c.source_id} is not a permitted source for {field} (docs/SOURCES.md)")
        for c in candidates
        if c.source_id not in order
    ]
    if not ranked:
        return Resolution(winner=None, losers=tuple(ignored))

    # Priority first, then newer observed_at within one source.
    ranked.sort(key=lambda c: (order.index(c.source_id), -c.observed_at.timestamp()))
    winner, rest = ranked[0], ranked[1:]

    losers: list[tuple[Candidate, str]] = []
    for candidate in rest:
        if candidate.source_id == winner.source_id:
            reason = f"same source ({candidate.source_id}), older observed_at"
        else:
            reason = (
                f"{winner.source_id} outranks {candidate.source_id} for {field} "
                "(docs/SOURCES.md priority table)"
            )
        losers.append((candidate, reason))
    losers.extend(ignored)

    return Resolution(
        winner=winner,
        losers=tuple(losers),
        disputed=is_disputed(field, winner, rest),
    )


def is_disputed(field: str, winner: Candidate, others: list[Candidate]) -> bool:
    """True when two sources disagree by more than 20% on a numeric field.

    Do not average and do not silently drop the loser (Project-Doc 6.7). The
    caller stores both, sets the flag, and raises a review_queue row.
    """
    if field not in DISPUTED_FLAGS:
        return False
    win = _number(winner.value)
    if win is None or win == 0:
        return False
    for other in others:
        if other.source_id == winner.source_id:
            continue
        value = _number(other.value)
        if value is None:
            continue
        if abs(value - win) / abs(win) > DISPUTE_THRESHOLD:
            return True
    return False


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
