"""Deterministic parser for the CISCE School Locator (S3). No LLM, no heuristics.

[VERIFIED 2026-09-15] against tests/fixtures/cisce/. One layout only - there is
no detail page, so everything this source has arrives in a single card:

    <td><div class="school-card">
      <div class="top-right-badges"> Co-ed. | Day/Residential </div>
      <h6 class="fw-bold"> AN001 - Don Bosco School </h6>
      <p class="mb-1"> {principal} <br> {address}, {district}, {state}, {pin}, India </p>
      <div class="school-badges"> ICSE | ISC | CVE </div>
      <div class="text-end"> <a href="...">Visit Website</a> </div>
    </div></td>

Extraction is class-driven, never positional. The two `.top-right-badges` spans
carry gender and day/residential in that order, but the second is sometimes
absent, so they are classified by their VALUE against the locator's own dropdown
vocabularies - not by index.
"""

import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

# [VERIFIED] The three affiliation badges. ISC is the class-12 one: a school
# with ICSE and no ISC stops at class 10 and fails the hard gate. CVE is the
# vocational certificate and implies nothing about class 12.
ICSE = "ICSE"
ISC = "ISC"
CVE = "CVE"
AFFILIATIONS = (ICSE, ISC, CVE)

# [VERIFIED] Values of the locator's own `gender` and `duration` dropdowns.
# Note the trailing dot on "Co-ed." - it is in the source, not a typo.
GENDERS = ("Boys", "Girls", "Co-ed.")
CLASSIFICATIONS = ("Day/Residential", "Day/Boarding", "Residential", "Day")

_CODE_NAME = re.compile(r"^\s*([A-Z]{2,3}\d+)\s*-\s*(.+)$", re.DOTALL)
_PINCODE = re.compile(r"^\d{3}\s?\d{3}$")
_STATED_TOTAL = re.compile(
    r"of\s*<[^>]*>\s*([\d,]+)|of\s+([\d,]+)\s+entries", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class SchoolCard:
    """One institution as the locator renders it. There is nothing else to fetch."""

    #: State-prefixed and nationally unique, e.g. AN001. The prefix is
    #: HISTORICAL, not current geography - the AP0xx block is full of Telangana
    #: schools that predate the 2014 split. Never derive `state` from it.
    code: str
    name: str
    affiliations: tuple[str, ...] = ()
    principal_name: str | None = None
    address: str | None = None
    district: str | None = None
    state: str | None = None
    pincode: str | None = None
    country: str | None = None
    gender: str | None = None
    residential: str | None = None
    website: str | None = None

    @property
    def entity_key(self) -> str:
        return f"cisce:{self.code}"

    @property
    def has_class_12(self) -> bool:
        """ISC is class 12. ICSE alone is not - see docs/SOURCES.md S3."""
        return ISC in self.affiliations


@dataclass
class ResultPage:
    rows: list[SchoolCard] = field(default_factory=list)
    stated_total: int | None = None
    #: Cards present in the HTML that could not be parsed, with a reason.
    #: Counted, never silently dropped.
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def seen(self) -> int:
        return len(self.rows) + len(self.skipped)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", value).strip().strip(",")
    return value or None


def parse_address(blob: str) -> dict[str, str | None]:
    """Split the comma-joined address line. Parsed from the RIGHT.

    The left-hand side has no fixed field count - locality and district are
    sometimes one segment, sometimes two, sometimes four. Only the tail is
    reliable: `..., {state}, {pincode}, {country}`. So the tail is consumed from
    the end and whatever remains is the address, which is the honest answer
    rather than a positional guess (hard rule 6).

    Pincodes appear as both `744103` and `533 201`; the space is normalised out.
    """
    parts = [p for p in (_clean(p) for p in blob.split(",")) if p]
    out: dict[str, str | None] = {
        "country": None,
        "pincode": None,
        "state": None,
        "district": None,
        "address": None,
    }
    if not parts:
        return out

    # The locator serves five countries; anything non-numeric in the last slot
    # is one of them.
    if not _PINCODE.match(parts[-1]):
        out["country"] = parts.pop()
    if parts and _PINCODE.match(parts[-1]):
        out["pincode"] = parts.pop().replace(" ", "")
    if parts:
        out["state"] = parts.pop()
    if parts:
        out["district"] = parts.pop()
    out["address"] = ", ".join(parts) or None
    return out


def _parse_card(card) -> SchoolCard | None:
    heading = card.css_first("h6")
    if heading is None:
        return None
    m = _CODE_NAME.match(heading.text(separator=" ", strip=True))
    if m is None:
        return None
    code, name = m.group(1), _clean(m.group(2))
    if not name:
        return None

    gender = residential = None
    for span in card.css(".top-right-badges span"):
        value = span.text(strip=True)
        if value in GENDERS:
            gender = value
        elif value in CLASSIFICATIONS:
            residential = value

    affiliations = tuple(
        b
        for b in (s.text(strip=True) for s in card.css(".school-badges span"))
        if b in AFFILIATIONS
    )

    principal = address = None
    body = card.css_first("p")
    if body is not None:
        # The <br> is the only separator between principal and address, so the
        # fragment has to be split before its tags are stripped.
        fragments = (
            _clean(HTMLParser(chunk).text(separator=" "))
            for chunk in (body.html or "").split("<br>")
        )
        lines = [line for line in fragments if line]
        if lines:
            principal = lines[0]
        if len(lines) > 1:
            address = " ".join(lines[1:])

    link = card.css_first(".text-end a[href]")
    parts = parse_address(address or "")
    return SchoolCard(
        code=code,
        name=name,
        affiliations=affiliations,
        principal_name=principal,
        address=parts["address"],
        district=parts["district"],
        state=parts["state"],
        pincode=parts["pincode"],
        country=parts["country"],
        gender=gender,
        residential=residential,
        website=_clean(link.attributes.get("href")) if link is not None else None,
    )


def stated_total(html: str) -> int | None:
    """The page's own `Showing 1 to 10 of 1928 entries` count.

    Checked against the parsed row count by the importer. A silent shortfall
    here looks exactly like "CISCE lost four hundred schools this quarter".
    """
    m = _STATED_TOTAL.search(html)
    if m is None:
        return None
    return int((m.group(1) or m.group(2)).replace(",", ""))


def parse_result(html: str) -> ResultPage:
    tree = HTMLParser(html)
    page = ResultPage(stated_total=stated_total(html))
    for card in tree.css(".school-card"):
        parsed = _parse_card(card)
        if parsed is None:
            page.skipped.append(("?", card.text(separator=" ", strip=True)[:120]))
        else:
            page.rows.append(parsed)
    return page
