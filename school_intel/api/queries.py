"""Read-only queries behind the API. Postgres only.

Hard rule 3 / ARCHITECTURE Rule 1: a user request NEVER triggers a fetch or an
LLM call. Every function here is a SELECT. The "get more data" button does not
scrape - it writes a row to `jobs` and the worker does the fetching, so a click
returns instantly and the rate limits still hold.
"""

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

COORDS_PATH = Path(__file__).with_name("state_coords.yaml")
BOUNDARIES_PATH = Path(__file__).with_name("state_boundaries.json")

# India's bounding box, and the SVG canvas the bubbles are drawn on. Projecting
# real coordinates is what makes the bubbles form the actual shape of India
# instead of a hand-arranged diagram.
LAT_RANGE = (6.0, 37.5)
LON_RANGE = (68.0, 97.5)
CANVAS = (760, 820)


@lru_cache(maxsize=1)
def state_coords() -> dict[str, tuple[float, float]]:
    raw = yaml.safe_load(COORDS_PATH.read_text("utf-8"))
    return {name.lower(): (float(v[0]), float(v[1])) for name, v in raw.items()}


@lru_cache(maxsize=1)
def state_boundaries() -> list[str]:
    """SVG path outlines, decorative only. Pre-projected onto the same canvas as
    the bubbles (see `project`) so they line up without a client-side map
    library - generated once from public India state boundary data, not a
    dependency. Coastline only, holes dropped - a map background, not a source
    of truth for anything scored."""
    return json.loads(BOUNDARIES_PATH.read_text("utf-8"))


def project(lat: float, lon: float) -> tuple[float, float]:
    """Equirectangular. Good enough for bubbles; nobody navigates by this."""
    width, height = CANVAS
    x = (lon - LON_RANGE[0]) / (LON_RANGE[1] - LON_RANGE[0]) * width
    y = (LAT_RANGE[1] - lat) / (LAT_RANGE[1] - LAT_RANGE[0]) * height
    return round(x, 1), round(y, 1)


def bubble_radius(total: int, largest: int) -> float:
    """Area proportional to count, so a state with 10x the schools looks 10x
    bigger by area rather than 10x by radius (which would look like 100x)."""
    if largest <= 0 or total <= 0:
        return 4.0
    return round(6 + 34 * math.sqrt(total / largest), 1)


# Fields the sales team asked for, in the order they read them.
SCHOOL_COLUMNS = """
    i.id, i.canonical_name, i.district, i.city, i.state, i.metro_area,
    i.pincode, i.address, i.website, i.email, i.phone, i.email_guessed,
    i.fee_annual_inr_min, i.fee_annual_inr_max, i.fee_annual_inr_mid, i.fee_year,
    i.total_enrollment, i.enrollment_is_estimated, i.total_teachers,
    i.class_12_total, i.pcm_12_count, i.pcm_12_is_estimated,
    i.has_class_12, i.status, i.year_founded, i.legal_entity_name,
    (SELECT p.full_name FROM roles r JOIN people p ON p.id = r.person_id
      WHERE r.institution_id = i.id AND r.title_normalized = 'principal'
      ORDER BY r.id LIMIT 1) AS principal_name,
    (SELECT p.full_name FROM roles r JOIN people p ON p.id = r.person_id
      WHERE r.institution_id = i.id
        AND r.title_normalized IN ('career_counsellor','academic_coordinator')
      ORDER BY r.id LIMIT 1) AS counsellor_name,
    (SELECT string_agg(b.board, ', ' ORDER BY b.board) FROM institution_boards b
      WHERE b.institution_id = i.id) AS boards,
    sc.fit, sc.confidence, sc.flags,
    CASE
      WHEN i.phone IS NOT NULL OR i.email IS NOT NULL THEN 'verified'
      WHEN i.last_enriched_at IS NOT NULL AND i.website IS NOT NULL THEN 'weak'
      ELSE 'none'
    END AS contact_tier
"""

SCHOOL_FROM = """
    FROM institutions i
    LEFT JOIN LATERAL (
      SELECT s.fit, s.confidence, s.flags FROM scores s
       WHERE s.institution_id = i.id
       ORDER BY s.computed_at DESC LIMIT 1
    ) sc ON true
"""


@dataclass(frozen=True, slots=True)
class StateSummary:
    state: str
    total: int
    senior_secondary: int
    with_phone: int
    with_email: int
    with_fee: int
    with_students: int
    enriched: int

    @property
    def contactable(self) -> int:
        return max(self.with_phone, self.with_email)


def state_summary(session: Session) -> list[StateSummary]:
    """Per-state counts for the map. One query.

    `enriched` counts schools we have actually visited the website of, which is
    what the "get more" button increases. Without it the map cannot distinguish
    "few schools here" from "we have not looked here yet".
    """
    rows = session.execute(
        text("""
        SELECT coalesce(i.state, '(unknown)') AS state,
               count(*) AS total,
               count(*) FILTER (WHERE i.has_class_12) AS senior_secondary,
               count(*) FILTER (WHERE i.phone IS NOT NULL) AS with_phone,
               count(*) FILTER (WHERE i.email IS NOT NULL) AS with_email,
               count(*) FILTER (WHERE i.fee_annual_inr_mid IS NOT NULL) AS with_fee,
               count(*) FILTER (WHERE i.total_enrollment IS NOT NULL) AS with_students,
               count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM observations o
                    WHERE o.institution_id = i.id AND o.source_id = 'cbse_mpd'
               )) AS enriched
          FROM institutions i
         WHERE i.status = 'active'
         GROUP BY 1
         ORDER BY 2 DESC
        """)
    ).all()
    return [StateSummary(*row) for row in rows]


def map_points(session: Session) -> tuple[list[dict], list[str]]:
    """Everything the map needs: position, size, counts, a link, and the
    background state outlines (same projection, so they line up)."""
    summaries = state_summary(session)
    coords = state_coords()
    largest = max((s.total for s in summaries), default=0)
    points = []
    for summary in summaries:
        position = coords.get(summary.state.lower())
        if position is None:
            # A state we have no coordinate for is listed beside the map rather
            # than dropped - silently losing a state would look like zero
            # schools there.
            points.append({"state": summary.state, "summary": summary, "x": None})
            continue
        x, y = project(*position)
        points.append(
            {
                "state": summary.state,
                "summary": summary,
                "x": x,
                "y": y,
                "r": bubble_radius(summary.total, largest),
            }
        )
    return points, state_boundaries()


# ponytail: SQL fragments in a dict, not a filter-object hierarchy. Two facets,
# fixed vocabularies, validated by dict lookup - an unknown value falls back to
# "any" rather than reaching the query.
CONTACT_FILTERS = {
    "any": None,
    "verified": "(i.phone IS NOT NULL OR i.email IS NOT NULL)",
    "weak": (
        "(i.phone IS NULL AND i.email IS NULL"
        " AND i.last_enriched_at IS NOT NULL AND i.website IS NOT NULL)"
    ),
    "reachable": (
        "(i.phone IS NOT NULL OR i.email IS NOT NULL"
        " OR (i.last_enriched_at IS NOT NULL AND i.website IS NOT NULL))"
    ),
    "none": (
        "(i.phone IS NULL AND i.email IS NULL"
        " AND (i.last_enriched_at IS NULL OR i.website IS NULL))"
    ),
}

CLASS12_FILTERS = {
    "any": None,
    "yes": "i.has_class_12",
    # Not "no". A false here means no source has SHOWN us class 12, which is not
    # the same as the school having none - most of these were never enriched.
    "unconfirmed": "NOT i.has_class_12",
}


def schools_in_state(
    session: Session,
    state: str,
    limit: int = 200,
    only_contactable: bool = False,
    contact: str = "any",
    class12: str = "any",
) -> list[dict]:
    """Schools for a state, best-informed first.

    Ordering is by how much we KNOW, not by Fit. The sales team opens a state to
    find someone to call, so a school with a phone and an email belongs above a
    school with a higher Fit and no way in.
    """
    clauses = ["lower(i.state) = lower(:state)", "i.status = 'active'"]
    if only_contactable:
        contact = "verified"
    for table, key in ((CONTACT_FILTERS, contact), (CLASS12_FILTERS, class12)):
        clause = table.get(key)
        if clause:
            clauses.append(clause)
    rows = (
        session.execute(
            text(
                f"SELECT {SCHOOL_COLUMNS} {SCHOOL_FROM}"
                f" WHERE {' AND '.join(clauses)}"
                " ORDER BY ("
                "   (i.phone IS NOT NULL)::int + (i.email IS NOT NULL)::int"
                " + (i.fee_annual_inr_mid IS NOT NULL)::int"
                " + (i.total_enrollment IS NOT NULL)::int"
                " + (i.website IS NOT NULL)::int"
                " ) DESC, i.has_class_12 DESC, coalesce(sc.fit, 0) DESC, i.id"
                " LIMIT :limit"
            ),
            {"state": state, "limit": limit},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def schools_in_city(session: Session, city: str, limit: int = 200) -> list[dict]:
    """City search across city, district and metro, since sources disagree about
    which of the three a place name belongs to."""
    rows = (
        session.execute(
            text(
                f"SELECT {SCHOOL_COLUMNS} {SCHOOL_FROM}"
                " WHERE i.status = 'active' AND ("
                "     lower(i.city) = lower(:city)"
                "  OR lower(i.district) = lower(:city)"
                "  OR lower(coalesce(i.metro_area,'')) = lower(:city)"
                "  OR lower(i.district) LIKE lower(:like)"
                " )"
                " ORDER BY ("
                "   (i.phone IS NOT NULL)::int + (i.email IS NOT NULL)::int"
                " + (i.fee_annual_inr_mid IS NOT NULL)::int"
                " ) DESC, i.has_class_12 DESC, i.id"
                " LIMIT :limit"
            ),
            {"city": city, "like": f"%{city}%", "limit": limit},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


# Acronyms the BD team types instead of the full name. Only the ones that
# genuinely cannot be matched any other way: similarity('DELHI PUBLIC SCHOOL',
# 'dps') is 0.04, so neither the tsvector nor the trigram index can bridge these.
# Brands that are already words ("narayana", "ryan", "chaitanya") need no entry.
NAME_ALIASES = {
    "dps": "delhi public school",
    "kv": "kendriya vidyalaya",
    "kvs": "kendriya vidyalaya",
    "jnv": "jawahar navodaya vidyalaya",
    "dav": "dav public school",
    "nps": "national public school",
    "aps": "army public school",
}


def schools_by_name(session: Session, name: str, limit: int = 200) -> list[dict]:
    """Search school names, with the city/district/state already in search_tsv.

    `search_tsv` (GIN, 'simple') does the work. It already carries city,
    district and state, so "kendriya vidyalaya pune" narrows a 1,256-campus
    chain to one city, and plainto_tsquery ANDs the words so every word counts.

    ponytail: trigram typo-matching only for a SINGLE-word query, and only when
    the word match found nothing. A trigram match ignores extra words -
    similarity('KENDRIYA VIDYALAYA', 'kendriya vidyalaya chennai') is 0.69, over
    the 0.3 threshold - so on a multi-word query it silently defeats the place
    narrowing above and hands the BD team schools in the wrong city. One word
    has no place term to defeat. Cost: "delhi publik school" finds nothing.
    Upgrade path if typos turn out to matter: per-word `strict_word_similarity`
    rather than a similarity threshold, which does not separate the two cases
    (typo 0.42 vs wrong-city 0.42 against the same haystack - measured).

    Both indexes already exist (models.py:269-275), so this adds no migration.
    Exact substring matches sort first, otherwise a loose match that happens to
    carry a phone number would outrank the school actually typed.
    """
    query = NAME_ALIASES.get(name.strip().lower(), name)

    def run(match: str) -> list[dict]:
        rows = (
            session.execute(
                text(
                    f"SELECT {SCHOOL_COLUMNS} {SCHOOL_FROM}"
                    f" WHERE i.status = 'active' AND {match}"
                    " ORDER BY (i.canonical_name ILIKE :like) DESC,"
                    "   ("
                    "     (i.phone IS NOT NULL)::int + (i.email IS NOT NULL)::int"
                    "   + (i.fee_annual_inr_mid IS NOT NULL)::int"
                    "   ) DESC, i.has_class_12 DESC, i.id"
                    " LIMIT :limit"
                ),
                {"q": query, "like": f"%{query}%", "limit": limit},
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    found = run("i.search_tsv @@ plainto_tsquery('simple', :q)")
    if not found and len(query.split()) == 1:
        found = run("i.canonical_name % :q")
    return found


def known_cities(session: Session) -> list[str]:
    """Places we actually have schools for.

    Used to decide whether to offer the JustDial link at all: their URL silently
    redirects an unknown city to Mumbai, so offering it for a place we do not
    recognise would send the sales team to the wrong city's listings.
    """
    rows = (
        session.execute(
            text(
                "SELECT DISTINCT lower(district) FROM institutions"
                " WHERE district IS NOT NULL AND status = 'active'"
                " UNION"
                " SELECT DISTINCT lower(metro_area) FROM institutions"
                " WHERE metro_area IS NOT NULL"
            )
        )
        .scalars()
        .all()
    )
    return sorted(c for c in rows if c)


def coverage_totals(session: Session) -> dict:
    # Reuse the same tiers the state/city listings already filter by, so
    # "contactable" on the homepage means exactly what the state page's
    # "contact = reachable/verified" dropdown means.
    verified = CONTACT_FILTERS["verified"]
    reachable = CONTACT_FILTERS["reachable"]
    return dict(
        session.execute(
            text(f"""
            SELECT count(*) AS schools,
                   count(*) FILTER (WHERE has_class_12) AS senior_secondary,
                   count(*) FILTER (WHERE website IS NOT NULL) AS with_website,
                   count(*) FILTER (WHERE phone IS NOT NULL) AS with_phone,
                   count(*) FILTER (WHERE email IS NOT NULL) AS with_email,
                   count(*) FILTER (WHERE fee_annual_inr_mid IS NOT NULL) AS with_fee,
                   count(*) FILTER (WHERE total_enrollment IS NOT NULL) AS with_students,
                   count(*) FILTER (WHERE {reachable}) AS contactable,
                   count(*) FILTER (WHERE {verified}) AS strongly_contactable,
                   count(DISTINCT state) AS states
              FROM institutions i WHERE status = 'active'
            """)
        )
        .mappings()
        .one()
    )
