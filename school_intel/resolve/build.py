"""Observations -> canonical institutions. Full recompute is the normal mode.

Order is fixed and load-bearing (docs/DATA-MODEL.md rebuild contract):

    group by entity_key
      -> link by stable ID
      -> apply the per-field priority table
      -> apply merge_decisions   (in sorted order, so rebuilds are identical)
      -> apply overrides         (LAST, above every source)
      -> write canonical rows + search_tsv

`run_at` is PASSED IN, never now() inside. Determinism depends on it: property
(a) of the rebuild contract is that two consecutive rebuilds from identical
inputs produce identical canonical rows, and a now() anywhere in here breaks it.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from school_intel.extract.normalize import (
    match_key,
    normalize_city,
    normalize_domain,
    normalize_legal_entity,
    normalize_person,
    normalize_state,
    normalize_title,
)
from school_intel.resolve.conflict import (
    DISPUTED_FLAGS,
    Candidate,
    Resolution,
    resolve,
)
from school_intel.sources.cbse_mpd import SOURCE_ID as MPD_SOURCE_ID

log = logging.getLogger(__name__)

# entity_key prefix -> the institutions column that holds that stable ID.
# Each prefix maps to exactly one column, which makes the linkage pass
# deterministic by construction (docs/DATA-MODEL.md item 1).
STABLE_ID_COLUMNS: dict[str, str] = {
    "cbse": "cbse_affiliation_no",
    "udise": "udise_code",
    "cisce": "cisce_code",
    "ib": "ib_school_code",
    "caie": "cambridge_centre_no",
}

# observations.field -> institutions column, where the names differ or the value
# needs a cast. Fields absent here are not written to the canonical row.
DIRECT_COLUMNS = {
    "address",
    "pincode",
    "city",
    "district",
    "state",
    "metro_area",
    "website",
    "email",
    "phone",
    "medium",
    "management_type",
    "legal_entity_name",
    "gender",
    "residential",
    "status",
}
INT_COLUMNS = {
    "total_teachers",
    "total_enrollment",
    "enrollment_year",
    "class_12_total",
    "pcm_12_count",
    "pcm_12_count_year",
    "fee_year",
    "year_founded",
    "grade_low",
    "grade_high",
}


@dataclass
class RebuildResult:
    institutions: int
    observations_linked: int
    conflicts: int
    flags: dict[str, int] = dc_field(default_factory=dict)


def rebuild(session: Session, run_at: datetime) -> RebuildResult:
    """Truncate the canonical layer and replay it from observations.

    Never touches observations, fetches, raw_documents, signals, review_queue,
    overrides, merge_decisions or eval_gold (hard rule 4, ADR-009).
    """
    reset_derived(session)

    grouped = _load_observations(session)
    merges = _merge_groups(session)

    # A merge group shares one canonical row; its members' claims are pooled.
    canonical_of: dict[str, str] = {}
    for group in merges:
        # Lowest key wins, deterministically - the merge group must resolve to
        # the same primary on every rebuild.
        primary = min(group)
        for member in group:
            canonical_of[member] = primary

    pooled: dict[str, dict[str, list[Candidate]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for entity_key, by_field in grouped.items():
        target = canonical_of.get(entity_key, entity_key)
        for f, candidates in by_field.items():
            pooled[target][f].extend(candidates)

    conflicts = 0
    flag_counts: dict[str, int] = defaultdict(int)
    written = 0
    seen_ids: set[int] = set()

    for entity_key, by_field in pooled.items():
        resolutions = {f: resolve(f, c) for f, c in by_field.items()}
        values = {f: r.winner.value for f, r in resolutions.items() if r.winner}
        evidence_by_field = {
            f: (r.winner.evidence_span or "")
            for f, r in resolutions.items()
            if r.winner
        }
        flags = [
            DISPUTED_FLAGS[f]
            for f, r in resolutions.items()
            if r.disputed and f in DISPUTED_FLAGS
        ]
        for flag in flags:
            flag_counts[flag] += 1
        for f, r in resolutions.items():
            if r.disputed:
                conflicts += 1
                _raise_conflict(session, entity_key, f, r)

        institution_id = _upsert_institution(
            session, entity_key, values, run_at, evidence_by_field
        )
        if institution_id is None:
            continue
        written += 1
        seen_ids.add(institution_id)
        _write_boards(session, institution_id, entity_key, values)
        write_roles(session, institution_id, values, run_at)
        members = [k for k, v in canonical_of.items() if v == entity_key] or [
            entity_key
        ]
        _link_observations(session, institution_id, members)

    pruned = prune_institutions(session, seen_ids)
    if pruned:
        log.info("pruned %d institution(s) with no live observations", pruned)

    linked = session.execute(
        text("SELECT count(*) FROM observations WHERE institution_id IS NOT NULL")
    ).scalar_one()

    apply_overrides(session, run_at)
    apply_metro_areas(session)
    _write_search_vectors(session)

    return RebuildResult(
        institutions=written,
        observations_linked=linked,
        conflicts=conflicts,
        flags=dict(flag_counts),
    )


def reset_derived(session: Session) -> None:
    """Clear the PURELY derived tables. Institutions and groups are upserted.

    NOT `TRUNCATE institutions ... CASCADE`, which is what docs/DATA-MODEL.md's
    rebuild contract literally said. TRUNCATE CASCADE ignores ON DELETE rules
    and truncates every referencing table, so that statement deletes
    `observations` and `signals` - the append-only truth layer that hard rule 4
    exists to protect. It was measured: it wiped 17,628 observations.

    Institutions are instead upserted on their stable-ID column and pruned at
    the end. That also keeps institution ids stable across rebuilds, which
    matters because `signals.institution_id` is ON DELETE CASCADE and would
    otherwise lose its history every single run.
    """
    for table in ("scores", "roles", "people", "institution_boards"):
        session.execute(text(f"DELETE FROM {table}"))
    # Derived pointers back into the canonical layer; recomputed below.
    session.execute(
        text("UPDATE observations SET institution_id = NULL, group_id = NULL")
    )


def prune_institutions(session: Session, seen_ids: set[int]) -> int:
    """Delete canonical rows no source claims any more.

    This is the only place institutions are deleted, and it is deliberate: an
    institution with no live observation has genuinely left the corpus, so its
    scores and signals should go with it.
    """
    if not seen_ids:
        return 0
    result = session.execute(
        text("DELETE FROM institutions WHERE NOT (id = ANY(:keep))"),
        {"keep": list(seen_ids)},
    )
    return result.rowcount or 0


def _load_observations(session: Session) -> dict[str, dict[str, list[Candidate]]]:
    """Live observations, minus the ones a school's own document cannot own.

    An MPD document belongs to exactly one school, so a `cbse_mpd` document
    carrying observations for several schools was mis-attributed - `fetches` has
    no institution_id and the URL-to-website match behind it is not one-to-one
    (117 institutions record their website as `gmail.com`). Feeding those to the
    resolver puts one school's phone and fee on 127 others. They stay in the
    table, because hard rule 4 forbids deleting an observation; they are simply
    not treated as evidence. Hard rule 6: missing beats wrong.

    Scoped to `cbse_mpd` deliberately. A `cbse_saras` list page covers a whole
    state and legitimately yields observations for hundreds of schools - 41 such
    documents carry the bulk of the registry, and excluding them would empty the
    directory.
    """
    rows = session.execute(
        text(
            "SELECT id, entity_key, field, value_text, source_id, observed_at,"
            " evidence_span FROM observations"
            " WHERE superseded_by IS NULL AND value_text IS NOT NULL"
            "   AND (source_id <> :mpd OR content_hash IS NULL"
            "        OR content_hash NOT IN ("
            "             SELECT content_hash FROM observations"
            "              WHERE source_id = :mpd AND content_hash IS NOT NULL"
            "              GROUP BY content_hash"
            "             HAVING count(DISTINCT entity_key) > 1))"
            " ORDER BY entity_key, field, observed_at DESC, id"
        ),
        {"mpd": MPD_SOURCE_ID},
    ).all()
    grouped: dict[str, dict[str, list[Candidate]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for obs_id, entity_key, f, value, source_id, observed_at, evidence in rows:
        grouped[entity_key][f].append(
            Candidate(
                observation_id=obs_id,
                source_id=source_id,
                value=value,
                observed_at=observed_at,
                evidence_span=evidence,
            )
        )
    return grouped


def _merge_groups(session: Session) -> list[set[str]]:
    """Union-find over merge_decisions, replayed in SORTED order.

    Sort order is what makes repeated rebuilds identical - never iterate these
    in insertion order or in whatever order Postgres returns them
    (docs/DATA-MODEL.md item 1).
    """
    rows = session.execute(
        text(
            "SELECT entity_key_a, entity_key_b FROM merge_decisions"
            " WHERE decision = 'same' ORDER BY entity_key_a, entity_key_b"
        )
    ).all()
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in rows:
        ra, rb = find(a), find(b)
        if ra != rb:
            lo, hi = sorted((ra, rb))
            parent[hi] = lo

    groups: dict[str, set[str]] = defaultdict(set)
    for key in parent:
        groups[find(key)].add(key)
    return [g for g in groups.values() if len(g) > 1]


def _upsert_institution(
    session: Session,
    entity_key: str,
    values: dict[str, str],
    run_at: datetime,
    evidence_by_field: dict[str, str] | None = None,
) -> int | None:
    prefix = entity_key.split(":", 1)[0]
    column = STABLE_ID_COLUMNS.get(prefix)
    if column is None:
        log.warning("entity_key %r has no stable-ID column; skipping", entity_key)
        return None
    stable_id = entity_key.split(":", 1)[1]

    name = values.get("name")
    if not name:
        return None

    row: dict[str, Any] = {
        "canonical_name": name,
        "institution_type": values.get("institution_type", "school"),
        "has_class_12": _as_bool(values.get("has_class_12")),
        "legal_entity_norm": normalize_legal_entity(values.get("legal_entity_name")),
        "first_seen_at": run_at,
        "last_resolved_at": run_at,
        "status": values.get("status", "active"),
    }
    for f in DIRECT_COLUMNS:
        if f in values and f not in row:
            row[f] = values[f]
    for f in INT_COLUMNS:
        if f in values:
            row[f] = _as_int(values[f])

    # `fee_annual_inr` is ONE observation field but TWO columns. Without this
    # mapping the extracted fees never reach the canonical layer and every
    # institution scores fee_unverified - which is exactly what happened: 49
    # real fees sat in `observations` while scoring reported 572 unknowns.
    fee = _as_int(values.get("fee_annual_inr"))
    if fee is not None:
        # A single published figure is both ends of the range. A genuine
        # min/max split needs a component breakdown, which is M2-2's
        # fee_component_breakdown and not available for most schools.
        row["fee_annual_inr_min"] = fee
        row["fee_annual_inr_max"] = fee

    # An approximated enrollment is flagged so the UI and export render it as
    # "~N (estimated)". The marker travels in the observation's evidence, which
    # is also what /why shows, so the two can never disagree.
    from school_intel.extract.mpd.tables import ESTIMATED_MARKER

    enrollment_evidence = (evidence_by_field or {}).get("total_enrollment", "")
    if values.get("total_enrollment") and ESTIMATED_MARKER in enrollment_evidence:
        row["enrollment_is_estimated"] = True

    streams = values.get("streams")
    if streams:
        row["streams"] = [s.strip() for s in streams.split(",") if s.strip()]

    row["city"] = normalize_city(values.get("city") or values.get("district"))
    row["state"] = normalize_state(values.get("state"))
    row["district"] = normalize_city(values.get("district"))
    if values.get("website"):
        row["website"] = normalize_domain(values["website"]) or values["website"]

    columns = ", ".join([column, *row.keys()])
    placeholders = ", ".join([":stable_id", *(f":{k}" for k in row)])
    updates = ", ".join(f"{k} = EXCLUDED.{k}" for k in row)
    return session.execute(
        text(
            f"INSERT INTO institutions ({columns}) VALUES ({placeholders})"
            f" ON CONFLICT ({column}) DO UPDATE SET {updates}"
            " RETURNING id"
        ),
        {"stable_id": stable_id, **row},
    ).scalar_one()


def _write_boards(
    session: Session, institution_id: int, entity_key: str, values: dict[str, str]
) -> None:
    """institution_boards is many-to-many; board filters are EXISTS subqueries.

    The board is implied by the registry that observed the institution, since a
    school only appears in the CBSE list because it holds a CBSE affiliation.
    """
    prefix = entity_key.split(":", 1)[0]
    board = {
        "cbse": "CBSE",
        "cisce": "CISCE_ISC",
        "ib": "IB_DP",
        "caie": "CAIE_ALEVEL",
    }.get(prefix)
    if board is None:
        return
    session.execute(
        text(
            "INSERT INTO institution_boards (institution_id, board, status, valid_from, valid_to)"
            " VALUES (:i, :b, :s, :vf, :vt)"
            " ON CONFLICT (institution_id, board) DO UPDATE"
            " SET status = EXCLUDED.status, valid_from = EXCLUDED.valid_from,"
            "     valid_to = EXCLUDED.valid_to"
        ),
        {
            "i": institution_id,
            "b": board,
            "s": "expired" if values.get("status") == "disaffiliated" else "active",
            "vf": _as_date(values.get("board_valid_from")),
            "vt": _as_date(values.get("board_valid_to")),
        },
    )


def write_roles(
    session: Session, institution_id: int, values: dict[str, str], run_at: datetime
) -> None:
    """People and roles for the named staff we have.

    Without this the `roles` table stays empty, and accessibility scoring can
    never award the principal (3 points) or counsellor (4 points) lines - 7 of
    its 10 points were unreachable for every school despite 1,847 principal
    names sitting in `observations`.

    title_raw is kept verbatim alongside the normalised title (Project-Doc 6.8):
    the raw string is evidence, the normalised one is only for filtering.
    """
    for field_name, raw_title in (
        ("principal_name", values.get("principal_title") or "Principal"),
        ("counsellor_name", values.get("counsellor_title") or "Career Counsellor"),
    ):
        full_name = values.get(field_name)
        if not full_name:
            continue
        normalised = normalize_person(full_name)
        if not normalised:
            continue
        person_id = session.execute(
            text(
                "INSERT INTO people (full_name, normalized_name) VALUES (:f, :n)"
                " RETURNING id"
            ),
            {"f": full_name[:200], "n": normalised[:200]},
        ).scalar_one()
        session.execute(
            text(
                "INSERT INTO roles (person_id, institution_id, title_raw,"
                " title_normalized, verified_at) VALUES (:p, :i, :raw, :norm, :at)"
            ),
            {
                "p": person_id,
                "i": institution_id,
                "raw": raw_title[:200],
                "norm": normalize_title(raw_title),
                # The registry republished this name in this run, which is what
                # keeps a stable principal out of stale_leadership.
                "at": run_at,
            },
        )


def _link_observations(
    session: Session, institution_id: int, entity_keys: list[str]
) -> None:
    session.execute(
        text(
            "UPDATE observations SET institution_id = :i"
            " WHERE entity_key = ANY(:keys) AND entity_type = 'institution'"
        ),
        {"i": institution_id, "keys": entity_keys},
    )


def _raise_conflict(session: Session, entity_key: str, f: str, r: Resolution) -> None:
    """Store both values with attribution rather than averaging (6.7)."""
    session.execute(
        text(
            "INSERT INTO review_queue (kind, payload) VALUES ('conflict', cast(:p as jsonb))"
        ),
        {
            "p": _json(
                {
                    "entity_key": entity_key,
                    "field": f,
                    "winner": {
                        "source_id": r.winner.source_id,
                        "value": r.winner.value,
                        "observation_id": r.winner.observation_id,
                    },
                    "others": [
                        {
                            "source_id": c.source_id,
                            "value": c.value,
                            "observation_id": c.observation_id,
                            "lost_because": why,
                        }
                        for c, why in r.losers
                    ],
                }
            )
        },
    )


def apply_overrides(session: Session, run_at: datetime) -> int:
    """Human corrections, applied LAST and above every source.

    These MUST survive every rebuild - that is the entire point of the table.
    """
    rows = session.execute(
        text(
            "SELECT entity_type, entity_id, field, value_json FROM overrides"
            " WHERE expires_at IS NULL OR expires_at > :now"
            " ORDER BY entity_type, entity_id, field"
        ),
        {"now": run_at},
    ).all()
    applied = 0
    for entity_type, entity_id, f, value_json in rows:
        if entity_type != "institution":
            continue
        # excluded / priority_boost are control fields, not columns; scoring
        # reads them straight from `overrides` (docs/DATA-MODEL.md item 4).
        if f in {"excluded", "priority_boost"}:
            continue
        value = (
            value_json if not isinstance(value_json, dict) else value_json.get("value")
        )
        session.execute(
            text(f"UPDATE institutions SET {f} = :v WHERE id = :i"),
            {"v": value, "i": entity_id},
        )
        applied += 1
    return applied


def apply_metro_areas(session: Session) -> int:
    """Tag every institution whose PIN falls in a metro (M1-7).

    Config-only, so it runs on every rebuild with no external file. lat/lon needs
    the India Post CSV and is applied separately by `cli load-pins`.
    """
    from school_intel.sources.pin_centroids import metro_for_pin

    rows = session.execute(
        text("SELECT id, pincode FROM institutions WHERE pincode IS NOT NULL")
    ).all()
    tagged = 0
    for institution_id, pincode in rows:
        metro = metro_for_pin(pincode)
        if metro is None:
            continue
        session.execute(
            text("UPDATE institutions SET metro_area = :m WHERE id = :i"),
            {"m": metro, "i": institution_id},
        )
        tagged += 1
    return tagged


def _write_search_vectors(session: Session) -> None:
    """Set search_tsv in the resolver, not a trigger, so a rebuild stays a pure
    function of its inputs. 'simple', not 'english' - stemming mangles
    Vidyalaya / Bhavan / Vidya Mandir and actively harms recall."""
    session.execute(
        text(
            "UPDATE institutions SET search_tsv = to_tsvector('simple',"
            " coalesce(canonical_name,'') || ' ' || coalesce(city,'') || ' ' ||"
            " coalesce(district,'') || ' ' || coalesce(state,'') || ' ' ||"
            " coalesce(legal_entity_name,''))"
        )
    )


def _as_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.strip().lower() in {"true", "t", "yes", "1"}


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def _as_date(value: str | None) -> str | None:
    """SARAS publishes dd/mm/yyyy. Anything else is left unparsed rather than
    guessed at - a wrong affiliation expiry would look like a disaffiliation."""
    if not value:
        return None
    parts = value.strip().split("/")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        d, m, y = parts
        return f"{y}-{m}-{d}"
    return None


def _json(payload: dict) -> str:
    import json

    return json.dumps(payload)


# `normalize_person` and `match_key` are imported for use by M4-1 blocking and
# by role extraction; referenced here so the import is not flagged as unused.
__all__ = [
    "RebuildResult",
    "apply_overrides",
    "match_key",
    "normalize_person",
    "prune_institutions",
    "rebuild",
    "reset_derived",
]
