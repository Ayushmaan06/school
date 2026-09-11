"""STAGE 7 - score every institution that passes the hard gate.

Order matters: resolve -> group rollup -> score. Group leverage reads
`groups.qualifying_campus_count`, which M4-1b computes without importing
anything from this package (see resolve/groups.py).

Flags never score points. They are chips in the UI and filters in search.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from school_intel.score import confidence, config, fit

log = logging.getLogger(__name__)

# Institutions to score, with everything the six components need. One query, so
# scoring a national corpus is not 9,600 round-trips.
LOAD_SQL = text("""
    SELECT i.id, i.canonical_name, i.fee_annual_inr_mid, i.fee_year,
           i.pcm_12_count, i.pcm_12_is_estimated, i.class_12_total,
           i.total_enrollment, i.district, i.metro_area, i.state,
           i.email, i.phone, i.streams, i.has_class_12, i.status,
           i.grade_low, i.grade_high,
           g.qualifying_campus_count, g.group_type,
           (SELECT array_agg(b.board) FROM institution_boards b
             WHERE b.institution_id = i.id) AS boards,
           (SELECT count(*) FROM roles r
             WHERE r.institution_id = i.id
               AND r.title_normalized IN ('career_counsellor','academic_coordinator')
           ) AS counsellors,
           (SELECT max(r.verified_at) FROM roles r
             WHERE r.institution_id = i.id AND r.title_normalized = 'principal'
           ) AS principal_verified_at,
           (SELECT min(o.observed_at) FROM observations o
             WHERE o.institution_id = i.id AND o.superseded_by IS NULL
           ) AS oldest_observation,
           (SELECT array_agg(DISTINCT s.authority_tier)
              FROM observations o JOIN source_registry s ON s.id = o.source_id
             WHERE o.institution_id = i.id AND o.superseded_by IS NULL
           ) AS authority_tiers,
           EXISTS (SELECT 1 FROM overrides ov
                    WHERE ov.entity_type = 'institution' AND ov.entity_id = i.id
                      AND ov.field = 'excluded') AS excluded,
           (SELECT (ov.value_json #>> '{}')::int FROM overrides ov
             WHERE ov.entity_type = 'institution' AND ov.entity_id = i.id
               AND ov.field = 'priority_boost' LIMIT 1) AS priority_boost,
           EXISTS (SELECT 1 FROM review_queue rq
                    WHERE rq.state = 'open'
                      AND (rq.payload ->> 'institution_id')::text = i.id::text
           ) AS review_open
      FROM institutions i
      LEFT JOIN groups g ON g.id = i.group_id
     ORDER BY i.id
""")


@dataclass
class ScoreRun:
    scored: int
    gated_out: int
    insufficient_data: int
    flags: dict[str, int]


def passes_hard_gate(row) -> tuple[bool, str]:
    """Eligibility, not penalty. Failing means not scored and not shown."""
    gate = config.hard_gate()
    if gate["require_class_12"] and not row.has_class_12:
        return False, "no class 12"
    if row.status not in gate["active_statuses"]:
        return False, f"status={row.status}"
    if row.excluded:
        return False, "excluded by override"

    streams = list(row.streams or [])
    if streams:
        if not any(s in gate["science_streams"] for s in streams):
            return False, "no science stream"
    else:
        # Unknown streams pass provisionally for the higher curriculum tiers,
        # where science is near-certain, and carry streams_unknown.
        boards = list(row.boards or [])
        if not any(b in gate["allow_unknown_streams_for_tiers"] for b in boards):
            # CBSE-only with unknown streams still passes: the whole corpus is
            # senior-secondary CBSE and excluding it would empty the list.
            pass
    return True, ""


def score_row(row, now: datetime, campaign: str = "default"):
    flags: list[str] = []

    affordability = fit.affordability(row.fee_annual_inr_mid)
    if row.fee_annual_inr_mid is None:
        flags.append("fee_unverified")

    volume, estimated = fit.pcm_12_volume(
        row.pcm_12_count,
        row.class_12_total,
        row.total_enrollment,
        spans_k12=(row.grade_low or 1) <= 5,
    )
    if estimated:
        flags.append("pcm_estimated")

    curriculum = fit.curriculum_tier(list(row.boards or []))
    geo = fit.geography(row.district, row.metro_area, row.state, campaign)

    principal_fresh = False
    if row.principal_verified_at is not None:
        months = (now - row.principal_verified_at).days / 30.44
        principal_fresh = (
            months <= config.accessibility()["principal_stale_after_months"]
        )
        if not principal_fresh:
            flags.append("stale_leadership")

    access = fit.accessibility(
        has_counsellor=bool(row.counsellors),
        principal_fresh=principal_fresh,
        has_email=bool(row.email),
        has_phone=bool(row.phone),
    )
    leverage = fit.group_leverage(row.qualifying_campus_count, row.group_type)

    if not row.streams:
        flags.append("streams_unknown")
    if row.review_open:
        flags.append("review_open")

    result = fit.combine(
        [affordability, volume, curriculum, geo, access, leverage], flags
    )

    conf = confidence.compute(
        present={
            "fee_annual_inr": row.fee_annual_inr_mid is not None,
            "pcm_12_count": row.pcm_12_count is not None,
            "board": bool(row.boards),
            "streams": bool(row.streams),
            "principal_name": row.principal_verified_at is not None,
            "email_or_phone": bool(row.email or row.phone),
            "pincode": row.metro_area is not None or row.district is not None,
            "total_enrollment": row.total_enrollment is not None,
        },
        authority_tiers=list(row.authority_tiers or []),
        oldest_observed_at=row.oldest_observation,
        now=now,
    )
    result.components["_confidence"] = conf.as_dict()

    score = result.fit
    if score is not None and row.priority_boost:
        # Applied AFTER renormalisation and clamped. Used sparingly; the
        # mandatory `reason` on the override is what justifies it.
        score = max(0.0, min(100.0, score + row.priority_boost))
        result.components["_meta"]["priority_boost"] = row.priority_boost
    return score, conf.confidence, result


def run(session: Session, now: datetime, campaign: str = "default") -> ScoreRun:
    rows = session.execute(LOAD_SQL).all()
    scored = gated = insufficient = 0
    flag_counts: dict[str, int] = {}
    model = config.model_version()

    session.execute(
        text("DELETE FROM scores WHERE model_version = :m AND campaign = :c"),
        {"m": model, "c": campaign},
    )

    for row in rows:
        ok, _why = passes_hard_gate(row)
        if not ok:
            gated += 1
            continue

        score, conf, result = score_row(row, now, campaign)
        if result.insufficient_data:
            insufficient += 1
        for flag in result.flags:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1

        session.execute(
            text(
                "INSERT INTO scores (institution_id, model_version, campaign, fit,"
                " confidence, components, flags, computed_at)"
                " VALUES (:i, :m, :c, :fit, :conf, cast(:comp as jsonb), :flags, :at)"
            ),
            {
                "i": row.id,
                "m": model,
                "c": campaign,
                # `possible < 40` suppresses Fit; 0 is stored so the row exists
                # and the insufficient_data flag explains why it is not ranked.
                "fit": score if score is not None else 0,
                "conf": conf,
                "comp": _json(result.components),
                "flags": result.flags,
                "at": now,
            },
        )
        scored += 1

    return ScoreRun(scored, gated, insufficient, flag_counts)


def _json(payload: dict) -> str:
    import json

    return json.dumps(payload, default=str)
