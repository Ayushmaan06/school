"""Group resolution and rollup (ADR-005, ADR-008, M4-1b).

M4-1b exists to break a real circular dependency. Scoring's group-leverage
component reads `groups.qualifying_campus_count`; if "qualifying" were
implemented as "scores well", scoring would depend on itself and the pipeline
would either deadlock or silently produce garbage on the first run.

**"Qualifying" does NOT mean "scores well."** A campus qualifies on:

  * the hard gate (has class 12, active), and
  * the affordability floor: fee midpoint >= the configured floor, OR fee
    unknown while the curriculum tier is CISCE_ISC or above

Nothing else. No Fit, no Confidence, no ranking.

NOTHING FROM THE SCORING PACKAGE MAY BE IMPORTED HERE. The floor is read out of
`scoring.yaml` by path, because hard rule 8 says the number lives in that file -
but reading a config file is not importing the scoring code. M4-1b's DoD greps
this package for such an import and it must find none, so this docstring
deliberately avoids spelling the import out.
"""

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

SCORING_PATH = Path(__file__).parents[1] / "score" / "scoring.yaml"

# A legal entity holding at least this many campuses is a group. One campus is
# just a school with a trust behind it.
MIN_CAMPUSES = 2


@lru_cache(maxsize=1)
def _qualifying_config() -> dict:
    return yaml.safe_load(SCORING_PATH.read_text("utf-8"))["qualifying_campus"]


def fee_floor() -> int:
    return int(_qualifying_config()["fee_floor_inr"])


def unknown_fee_tiers() -> tuple[str, ...]:
    return tuple(_qualifying_config()["fee_unknown_requires_tier"])


@dataclass
class RollupResult:
    groups: int
    institutions_assigned: int
    qualifying_campuses: int


def build_groups(session: Session) -> RollupResult:
    """Create groups from shared legal entities, then roll up their counts.

    ADR-005: the CBSE trust/society field is the legal-entity axis, which makes
    this an authoritative join rather than fuzzy brand matching. Two schools
    sharing a normalised trust name ARE one owner; two schools sharing a brand
    are not (that is `possible_franchise_network`, and it is not created here).
    """
    session.execute(text("UPDATE institutions SET group_id = NULL"))
    session.execute(text("DELETE FROM groups"))

    rows = session.execute(
        text(
            "SELECT legal_entity_norm, count(*) AS campuses,"
            "       min(legal_entity_name) AS display_name"
            " FROM institutions"
            " WHERE legal_entity_norm IS NOT NULL AND legal_entity_norm <> ''"
            " GROUP BY legal_entity_norm"
            " HAVING count(*) >= :minimum"
            " ORDER BY legal_entity_norm"
        ),
        {"minimum": MIN_CAMPUSES},
    ).all()

    created = assigned = 0
    for legal_norm, _campuses, display_name in rows:
        group_id = session.execute(
            text(
                "INSERT INTO groups (canonical_name, group_type, legal_entity_name,"
                " legal_entity_norm, evidence)"
                " VALUES (:name, 'verified_single_owner', :legal_name, :legal_norm,"
                "         :evidence) RETURNING id"
            ),
            {
                "name": display_name,
                "legal_name": display_name,
                "legal_norm": legal_norm,
                # Mandatory: how we concluded this group exists.
                "evidence": (
                    "shared normalised trust/society name from cbse_saras "
                    f"detail pages (ADR-005): {legal_norm!r}"
                ),
            },
        ).scalar_one()
        created += 1
        result = session.execute(
            text(
                "UPDATE institutions SET group_id = :gid"
                " WHERE legal_entity_norm = :norm"
            ),
            {"gid": group_id, "norm": legal_norm},
        )
        assigned += result.rowcount or 0

    qualifying = rollup_counts(session)
    return RollupResult(
        groups=created, institutions_assigned=assigned, qualifying_campuses=qualifying
    )


def rollup_counts(session: Session) -> int:
    """Populate campus_count, qualifying_campus_count and states_present.

    Returns the total number of qualifying campuses across all groups.
    """
    session.execute(
        text(
            "UPDATE groups g SET"
            " campus_count = sub.campuses,"
            " states_present = sub.states,"
            " hq_state = sub.hq_state"
            " FROM ("
            "   SELECT group_id, count(*) AS campuses,"
            "          array_agg(DISTINCT state) FILTER (WHERE state IS NOT NULL)"
            "            AS states,"
            "          mode() WITHIN GROUP (ORDER BY state) AS hq_state"
            "   FROM institutions WHERE group_id IS NOT NULL GROUP BY group_id"
            " ) sub WHERE g.id = sub.group_id"
        )
    )

    # The affordability floor. A Narayana branch charging Rs 30k/yr is Tier D and
    # must not inflate chain leverage (docs/SOURCES.md S7).
    qualifying_sql = """
        UPDATE groups g
           SET qualifying_campus_count = coalesce(sub.qualifying, 0)
          FROM (
            SELECT i.group_id, count(*) AS qualifying
              FROM institutions i
             WHERE i.group_id IS NOT NULL
               AND i.has_class_12
               AND i.status = 'active'
               AND (
                     i.fee_annual_inr_mid >= :floor
                  OR (i.fee_annual_inr_mid IS NULL AND EXISTS (
                        SELECT 1 FROM institution_boards b
                         WHERE b.institution_id = i.id
                           AND b.board = ANY(:tiers)
                     ))
               )
             GROUP BY i.group_id
          ) sub
         WHERE g.id = sub.group_id
    """
    session.execute(
        text(qualifying_sql), {"floor": fee_floor(), "tiers": list(unknown_fee_tiers())}
    )
    session.execute(
        text(
            "UPDATE groups SET qualifying_campus_count = 0"
            " WHERE qualifying_campus_count IS NULL"
        )
    )
    return (
        session.execute(
            text("SELECT coalesce(sum(qualifying_campus_count), 0) FROM groups")
        ).scalar_one()
        or 0
    )
