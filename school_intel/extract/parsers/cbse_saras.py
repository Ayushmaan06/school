"""Deterministic parsers for CBSE SARAS (S1). No LLM, no heuristics.

Two list layouts and one detail layout, all [VERIFIED M0-0] against saved
fixtures in tests/fixtures/cbse_saras/:

  state-wise (POST)  S No | Aff. No & School Code | State & District | Status
                     | School & Head Name | Address | Details
  closed (GET ID1=D) S No | Reg No. | State | District | Status | School Name
                     | Affiliation Status | Region | Details
  detail             two-cell label/value rows

Extraction is label-driven, never positional: find the cell whose text matches a
known label and read its neighbour. Column order changes between the two list
layouts, which is exactly why positional indexing would be a bug.
"""

import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

SENIOR_SECONDARY = "Senior Secondary Level"

# Values the "Status" / "School Status" column can take. Only the first has
# class 12, and that is the hard gate (docs/SCORING.md step 0).
SCHOOL_LEVELS = (SENIOR_SECONDARY, "Secondary Level", "Middle Class")


@dataclass(frozen=True, slots=True)
class ListRow:
    """One institution as it appears on a SARAS list page.

    The state-wise list carries far more than a bare directory: principal name,
    full address and website all arrive here, so most fields need no detail
    fetch at all (docs/SOURCES.md S1).
    """

    affiliation_no: str
    name: str
    state: str | None = None
    district: str | None = None
    status: str | None = None
    principal_name: str | None = None
    address: str | None = None
    website: str | None = None
    school_code: str | None = None
    affiliation_status: str | None = None
    region: str | None = None

    @property
    def entity_key(self) -> str:
        return f"cbse:{self.affiliation_no}"

    @property
    def has_class_12(self) -> bool:
        return self.status == SENIOR_SECONDARY


@dataclass
class ListPage:
    rows: list[ListRow] = field(default_factory=list)
    stated_total: int | None = None
    #: Rows present in the HTML that could not be turned into a ListRow, with a
    #: reason. Counted, never silently dropped - `stated_total` must reconcile
    #: against len(rows) + len(skipped) or the parser has drifted.
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def seen(self) -> int:
        return len(self.rows) + len(self.skipped)


def _cells(row) -> list[str]:
    return [c.text(separator=" ", strip=True) for c in row.css("td, th")]


def _split_labeled(blob: str, labels: tuple[str, ...]) -> dict[str, str]:
    """Split a cell that packs several 'Label : value' pairs into one string.

    The labels are passed in explicitly rather than discovered by a generic
    'next Capitalised word followed by a colon' lookahead. That shortcut looked
    fine and silently truncated addresses: in
    'Address : ... BANGALORE KARNATAKA Website : ...' the fragment
    'KARNATAKA Website :' matches "a capitalised label", so the address lost its
    state. Known labels cannot make that mistake.
    """
    positions: list[tuple[int, int, str]] = []
    for label in labels:
        m = re.search(rf"{re.escape(label)}\s*:", blob, re.IGNORECASE)
        if m:
            positions.append((m.start(), m.end(), label))
    positions.sort()

    out: dict[str, str] = {}
    for i, (_, value_start, label) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(blob)
        value = _clean(blob[value_start:end])
        if value:
            out[label] = value
    return out


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def parse_list(html: str) -> ListPage:
    """Parse either list layout. Which one is decided by the header row."""
    tree = HTMLParser(html)
    page = ListPage()

    page.stated_total = _stated_total(html)

    for row in tree.css("tr"):
        cells = _cells(row)
        if len(cells) < 6:
            continue
        joined = " | ".join(cells)
        if "S No" in cells[0] or cells[0].strip().lower() in {"s no", "sno", "s.no"}:
            continue  # header

        if "Aff. No." in joined:
            parsed = _parse_state_wise(cells)
        elif len(cells) >= 9:
            parsed = _parse_closed(cells)
        else:
            continue
        if parsed is None:
            page.skipped.append((cells[1] if len(cells) > 1 else "?", joined[:120]))
        else:
            page.rows.append(parsed)
    return page


# The result count. NOTE the label is unreliable: [VERIFIED M0-0] a state-wise
# *affiliated* query renders "Total No Of DisAffiliated Schools : 1847", because
# SARAS reuses one partial for every query. Only the banner
# "Total No. of Schools affiliated with CBSE" is reliably itself, so the result
# count is the other one. Never branch on that label's wording.
_BANNER = re.compile(r"Total No\.? of Schools affiliated with CBSE", re.IGNORECASE)


def _stated_total(html: str) -> int | None:
    totals = []
    for m in re.finditer(r"(Total No[^0-9]{0,120}?)(\d[\d,]*)", html, re.IGNORECASE):
        label, value = m.group(1), int(m.group(2).replace(",", ""))
        totals.append((bool(_BANNER.search(label)), value))
    non_banner = [v for is_banner, v in totals if not is_banner]
    if non_banner:
        return non_banner[-1]
    return totals[-1][1] if totals else None


def _parse_state_wise(cells: list[str]) -> ListRow | None:
    ids = _split_labeled(cells[1], ("Aff. No.", "Sch. Code"))
    geo = _split_labeled(cells[2], ("State", "District"))
    school = _split_labeled(cells[4], ("Name", "Head/Principal Name"))
    contact = _split_labeled(cells[5], ("Address", "Website"))

    affiliation_no = ids.get("Aff. No.", "")
    if not affiliation_no.isdigit():
        return None
    name = school.get("Name")
    if not name:
        return None

    return ListRow(
        affiliation_no=affiliation_no,
        school_code=ids.get("Sch. Code"),
        name=name,
        state=geo.get("State"),
        district=geo.get("District"),
        status=_clean(cells[3]),
        principal_name=school.get("Head/Principal Name"),
        address=contact.get("Address"),
        website=_normalize_site(contact.get("Website")),
    )


def _parse_closed(cells: list[str]) -> ListRow | None:
    # S No | Reg No. | State | District | Status | School Name | Affiliation Status | Region
    reg = _clean(cells[1])
    # [VERIFIED M0-0] The live closed list contains a junk row:
    #   400 | XXXXXXX | DELHI | NEW DELHI | Senior Secondary Level | TEST SCHOOL DEMO
    # A non-numeric registration number cannot form a valid entity_key, so the
    # row is skipped and counted rather than guessed at (hard rule 6).
    if not reg or not reg.isdigit():
        return None
    return ListRow(
        affiliation_no=reg,
        state=_clean(cells[2]),
        district=_clean(cells[3]),
        status=_clean(cells[4]),
        name=_clean(cells[5]) or "",
        affiliation_status=_clean(cells[6]),
        region=_clean(cells[7]) if len(cells) > 7 else None,
    )


def _normalize_site(value: str | None) -> str | None:
    value = _clean(value)
    if not value or "." not in value or value.lower() in {"na", "n/a", "nil", "-"}:
        return None
    return value


# --------------------------------------------------------------------------
# Detail page
# --------------------------------------------------------------------------

# Page label -> observations.field, per docs/SOURCES.md S1.
DETAIL_LABELS: dict[str, str] = {
    "name of institution": "name",
    "state": "state",
    "district": "district",
    "postal address": "address",
    "pin code": "pincode",
    "website": "website",
    "year of foundation": "year_founded",
    "name of principal/ head of institution": "principal_name",
    "status of the school": "grade_high",
    "school type": "residential",
    "name of trust/ society/ managing committee": "legal_entity_name",
}

# Deliberately NOT extracted:
#   "Principal's Educational/Professional Qualifications" - no decision value and
#     unnecessary personal data (COMPLIANCE.md purpose limitation)
#   "Gender" - [VERIFIED M0-0] this is the PRINCIPAL's gender, not the school's.
#     It sits directly under the principal name and reads like a school field.
SKIP_LABELS = frozenset(
    {
        "principal's educational/professional qualifications:",
        "gender",
        "administrative:",
        "teaching:",
        "remarks, if any",
    }
)


@dataclass(frozen=True, slots=True)
class DetailFields:
    values: dict[str, str]
    evidence: dict[str, str]


def parse_detail(html: str) -> DetailFields:
    """Label-driven extraction of the affiliation detail page.

    A missing label is a normal outcome: emit nothing rather than an empty
    observation (hard rule 6).
    """
    tree = HTMLParser(html)
    values: dict[str, str] = {}
    evidence: dict[str, str] = {}

    raw: dict[str, str] = {}
    for row in tree.css("tr"):
        cells = _cells(row)
        if len(cells) < 2:
            continue
        label = cells[0].strip().lower().rstrip(":").strip()
        value = _clean(cells[1])
        if not value:
            continue
        raw[label] = value
        if label in SKIP_LABELS or label.rstrip(":") in SKIP_LABELS:
            continue
        field_name = DETAIL_LABELS.get(label) or DETAIL_LABELS.get(label + ":")
        if field_name:
            values[field_name] = value
            evidence[field_name] = f"{cells[0].strip()}: {value}"

    # "Status of the School" carries the hard gate as well as grade_high.
    status = raw.get("status of the school")
    if status:
        values["has_class_12"] = "true" if status == SENIOR_SECONDARY else "false"
        evidence["has_class_12"] = f"Status of the School: {status}"

    # "Affiliation Period" splits into two dates. Formats vary; only accept the
    # documented one rather than guessing at anything else.
    period = _affiliation_period(tree)
    if period:
        start, end = period
        values["board_valid_from"], values["board_valid_to"] = start, end
        evidence["board_valid_from"] = evidence["board_valid_to"] = (
            f"Affiliation Period: From {start} To {end}"
        )

    return DetailFields(values=values, evidence=evidence)


_PERIOD = re.compile(
    r"From\s*:?\s*(\d{2}/\d{2}/\d{4})\s*To\s*:?\s*(\d{2}/\d{2}/\d{4})", re.IGNORECASE
)


def _affiliation_period(tree: HTMLParser) -> tuple[str, str] | None:
    m = _PERIOD.search(tree.text(separator=" "))
    return (m.group(1), m.group(2)) if m else None
