"""S9 - PIN code geography. Load once (ADR-004).

Two jobs:

1. PIN -> lat/lon, with `geo_precision='pin_centroid'`. Not exact coordinates,
   and deliberately so: ADR-004 rejected per-address geocoding for v1 because a
   PIN centroid is accurate enough for a distance-from-Bengaluru weighting and
   costs nothing.
2. PIN -> metro_area, the closed vocabulary scoring joins on. Longest prefix
   wins, which is what lets Delhi NCR span Delhi, Haryana and UP PINs while
   leaving the rest of those states unmapped (Project-Doc 6.2).

A PIN with no metro gets NULL and scores in the lowest geography band. That is
correct behaviour, not a gap to fill in.
"""

import csv
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

SOURCE_ID = "pin_centroids"
METRO_CONFIG = Path(__file__).with_name("metro_areas.yaml")

# Longest prefix in the config. Read once so lookup does not rescan.
_MAX_PREFIX = 6


@lru_cache(maxsize=1)
def _metro_index() -> dict[str, str]:
    """prefix -> metro_area, flattened for O(1) lookup per prefix length."""
    raw: dict[str, list[str]] = yaml.safe_load(METRO_CONFIG.read_text("utf-8"))
    index: dict[str, str] = {}
    for metro, prefixes in raw.items():
        for prefix in prefixes:
            prefix = str(prefix)
            if prefix in index and index[prefix] != metro:
                raise ValueError(
                    f"PIN prefix {prefix!r} is claimed by both {index[prefix]!r} "
                    f"and {metro!r}. A prefix maps to exactly one metro."
                )
            index[prefix] = metro
    return index


def metro_for_pin(pincode: str | None) -> str | None:
    """Longest-prefix-wins lookup. Returns None when the PIN is not in a metro."""
    if not pincode:
        return None
    digits = "".join(c for c in str(pincode) if c.isdigit())
    if len(digits) != 6:
        return None  # not a valid Indian PIN; do not guess
    index = _metro_index()
    for length in range(min(_MAX_PREFIX, len(digits)), 1, -1):
        metro = index.get(digits[:length])
        if metro is not None:
            return metro
    return None


@dataclass(frozen=True, slots=True)
class Centroid:
    pincode: str
    lat: float
    lon: float
    district: str | None
    state: str | None


def load_centroids(path: Path) -> list[Centroid]:
    """Read the India Post CSV.

    Column names vary between published copies of this dataset, so they are
    matched case-insensitively against a set of known aliases rather than by
    position. A row missing coordinates is skipped, never defaulted to (0, 0) -
    the Gulf of Guinea is not a plausible school location.
    """
    aliases = {
        "pincode": {"pincode", "pin_code", "pin", "postal_code"},
        "lat": {"latitude", "lat"},
        "lon": {"longitude", "lon", "long", "lng"},
        "district": {"district", "districtname", "district_name"},
        "state": {"statename", "state", "state_name", "circlename"},
    }
    out: list[Centroid] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return out
        lookup = {}
        for field in reader.fieldnames:
            key = field.strip().lower().replace(" ", "_")
            for target, names in aliases.items():
                if key in names:
                    lookup.setdefault(target, field)

        missing = {"pincode", "lat", "lon"} - set(lookup)
        if missing:
            raise ValueError(
                f"{path.name} is missing required column(s) {sorted(missing)}; "
                f"saw {reader.fieldnames}"
            )

        for row in reader:
            pin = "".join(c for c in (row.get(lookup["pincode"]) or "") if c.isdigit())
            if len(pin) != 6:
                continue
            try:
                lat = float(row[lookup["lat"]])
                lon = float(row[lookup["lon"]])
            except (TypeError, ValueError):
                continue
            # India's bounding box. A row outside it is corrupt, not a discovery.
            if not (6.0 <= lat <= 37.5 and 68.0 <= lon <= 97.5):
                continue
            out.append(
                Centroid(
                    pincode=pin,
                    lat=lat,
                    lon=lon,
                    district=(row.get(lookup.get("district", "")) or "").strip()
                    or None,
                    state=(row.get(lookup.get("state", "")) or "").strip() or None,
                )
            )
    return out


def apply_geography(session: Session, centroids: list[Centroid]) -> dict[str, int]:
    """Set lat/lon/geo_precision and metro_area on every institution with a PIN.

    Runs after resolve. Deliberately does NOT overwrite district or state: the
    doc's cross-check ("district comes from pin_centroids when the parsed value
    disagrees") needs the disagreement to be visible as an observation first, so
    that /why can explain it. Overwriting here would hide it.
    """
    by_pin = {c.pincode: c for c in centroids}
    rows = session.execute(
        text("SELECT id, pincode FROM institutions WHERE pincode IS NOT NULL")
    ).all()

    located = metro_tagged = 0
    for institution_id, pincode in rows:
        digits = "".join(c for c in (pincode or "") if c.isdigit())
        centroid = by_pin.get(digits)
        metro = metro_for_pin(digits)
        if centroid is None and metro is None:
            continue
        session.execute(
            text(
                "UPDATE institutions SET lat = :lat, lon = :lon,"
                " geo_precision = :prec, metro_area = :metro WHERE id = :id"
            ),
            {
                "lat": centroid.lat if centroid else None,
                "lon": centroid.lon if centroid else None,
                "prec": "pin_centroid" if centroid else None,
                "metro": metro,
                "id": institution_id,
            },
        )
        located += 1 if centroid else 0
        metro_tagged += 1 if metro else 0

    return {"with_pincode": len(rows), "located": located, "metro_tagged": metro_tagged}
