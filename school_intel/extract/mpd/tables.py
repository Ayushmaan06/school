"""Tier 1 - label and table parsing. No model inference, no API key (ADR-013).

Appendix-IX is a standardised statutory proforma with numbered rows and fixed
labels, so this finds the cell matching a known label and reads its neighbour.
That is faster, free and more reproducible than inference on exactly this shape
of input, and it makes the tune-and-rerun loop milliseconds rather than hours.

Two things this module refuses to do, both from hard rule 6:

  * guess a period. A fee with no stated period is not recorded.
  * take "the first number on the page". [VERIFIED M0-0] a nav-shell fee page
    yields a PIN code and a phone number as its only 5-6 digit numbers, so
    "nearest number to a fee keyword" emits a PIN code as an annual fee.
"""

import io
import logging
import re
from functools import lru_cache
from pathlib import Path

import yaml
from selectolax.parser import HTMLParser

from school_intel.extract.mpd.schemas import MPDExtraction, blocked_for_student_data
from school_intel.extract.normalize import normalize_person, parse_inr

log = logging.getLogger(__name__)

LABELS_PATH = Path(__file__).with_name("labels.yaml")
# parents[2] is the package root: mpd -> extract -> school_intel.
SCORING_PATH = Path(__file__).parents[2] / "score" / "scoring.yaml"
EXTRACTOR = "parser:mpd_table_v1"

#: Prefix written into a derived value's evidence_span. The resolver looks for
#: it to set institutions.enrollment_is_estimated, so an approximation is never
#: displayed as a published figure. Shared here so the two sides cannot drift.
ESTIMATED_MARKER = "estimated:"

# Under this many characters of extractable text, a PDF is a scan (ADR-016).
TEXT_LAYER_FLOOR = 50

# A result table: a marks/rank column header next to a name column. This is a
# STRUCTURAL check on table headers, never a word search over page text - the
# compliant proforma is titled "C: RESULT AND ACADEMICS", so a text-level rule
# would blocklist every Mandatory Disclosure page in India.
RESULT_COLUMN = re.compile(
    r"\b(roll|marks|percentage|percent|rank|grade|cgpa|score)\b", re.IGNORECASE
)
NAME_COLUMN = re.compile(r"\b(name|student|candidate)\b", re.IGNORECASE)


@lru_cache(maxsize=1)
def student_estimation() -> dict:
    """Read by path, not imported: extraction must not depend on scoring code."""
    return yaml.safe_load(SCORING_PATH.read_text("utf-8"))["student_estimation"]


@lru_cache(maxsize=1)
def labels() -> dict[str, list[str]]:
    raw: dict[str, list[str]] = yaml.safe_load(LABELS_PATH.read_text("utf-8"))
    return {field: [squash(v) for v in values] for field, values in raw.items()}


def squash(text: str) -> str:
    """Lowercase and remove ALL whitespace.

    [VERIFIED M0-0] OCR emits "XITOXII" and "MAY2024" with spaces dropped, so
    both sides of every label comparison are squashed. It also makes
    "Fee  Structure" and "FeeStructure" the same label for free.
    """
    return re.sub(r"\s+", "", text or "").lower()


def matches_label(cell: str, field: str) -> bool:
    target = squash(cell)
    return any(alias and alias in target for alias in labels().get(field, []))


# ---------------------------------------------------------------------------
# Document text
# ---------------------------------------------------------------------------


def pdf_rows(body: bytes) -> tuple[list[list[list[str]]], str]:
    """TABLES (not a flat row list) and full text from a PDF.

    Table boundaries are kept because they are what scopes a fee to a fee table.
    Layout IS the information; do not flatten a PDF to plain text.
    """
    import pdfplumber

    tables: list[list[list[str]]] = []
    chunks: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(body)) as doc:
            for page in doc.pages[:8]:
                chunks.append(page.extract_text() or "")
                for table in page.extract_tables() or []:
                    tables.append([[(c or "").strip() for c in row] for row in table])
    except Exception as exc:  # noqa: BLE001 - a malformed PDF is data, not a crash
        log.info("pdfplumber failed: %s", exc)
    return tables, "\n".join(chunks)


def html_rows(html: str) -> tuple[list[list[list[str]]], str]:
    """TABLES, not a flat row list. See pdf_rows."""
    tree = HTMLParser(html)
    for tag in ("script", "style", "nav", "footer"):
        for node in tree.css(tag):
            node.decompose()
    tables = [
        [
            [cell.text(separator=" ", strip=True) for cell in row.css("td, th")]
            for row in table.css("tr")
        ]
        for table in tree.css("table")
    ]
    loose = [
        [cell.text(separator=" ", strip=True) for cell in row.css("td, th")]
        for row in tree.css("tr")
        if row.parent is None or not tables
    ]
    if loose:
        tables.append(loose)
    return tables, tree.text(separator=" ")


def is_scanned_pdf(body: bytes, text: str) -> bool:
    return body[:5] == b"%PDF-" and len(text.strip()) < TEXT_LAYER_FLOOR


def detect_student_data(tables: list[list[list[str]]]) -> str | None:
    """Structural: a header row pairing a name column with a marks/rank column."""
    for table in tables:
        for row in table[:6]:
            if len(row) < 2:
                continue
            has_name = any(NAME_COLUMN.search(c) for c in row)
            has_result = any(RESULT_COLUMN.search(c) for c in row)
            if has_name and has_result:
                return f"result-table header: {' | '.join(c for c in row if c)[:200]}"
    return None


# ---------------------------------------------------------------------------
# Fee
# ---------------------------------------------------------------------------

_INT = re.compile(r"\d[\d,]*")

# A URL is not a source of money. [VERIFIED M4-2] "Academic%20Calendar%202026-27
# .pdf" yields the digit run "202026" because the percent-escape for a space
# merges with the year - and that was reported as a Rs 2,02,026 annual fee.
# Three of the top seven ranked schools had a fee like this.
_URLISH = re.compile(
    r"(?:https?://|www\.)\S+|\S+\.(?:pdf|docx?|xlsx?|jpe?g|png)\b", re.IGNORECASE
)
_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")


def strip_noise(cell: str) -> str:
    """Remove URLs and percent-escapes before looking for numbers."""
    cleaned = _URLISH.sub(" ", cell or "")
    return _ESCAPE.sub(" ", cleaned)


def _numbers(cell: str) -> list[int]:
    out = []
    for raw in _INT.findall(strip_noise(cell)):
        try:
            out.append(int(raw.replace(",", "")))
        except ValueError:
            continue
    return out


def is_not_money_row(row: list[str]) -> bool:
    """A row whose label means its numbers are identifiers, not rupees."""
    return any(matches_label(cell, "not_money_row") for cell in row[:2])


def total_column_index(header: list[str]) -> int | None:
    """[VERIFIED M0-0] Fees are published as instalments; the TOTAL column is the
    annual figure and the first column is a different affordability band."""
    for index, cell in enumerate(header):
        if matches_label(cell, "fee_total_column"):
            return index
    return None


def total_labelled_row(table: list[list[str]]) -> tuple[int, str] | None:
    """A row whose label says it is the TOTAL, e.g. "Total Fees | 182000".

    Component breakdowns list tuition, food, miscellaneous and so on as separate
    rows and then publish a total. Taking the largest component instead of the
    total is a wrong value, not a missing one.
    """
    for row in table:
        if not row or is_not_money_row(row):
            continue
        if not matches_label(row[0], "fee_total_row"):
            continue
        values = [n for cell in row[1:] for n in _numbers(cell)]
        plausible = [n for n in values if 1_000 <= n <= 5_000_000]
        if plausible:
            return max(plausible), " ".join(c for c in row if c)
    return None


def is_fee_table(table: list[list[str]]) -> bool:
    """Does this table actually publish fees?

    THE most important guard in this module. Without it, "largest plausible
    number in the document" reads the CBSE AFFILIATION NUMBER off the proforma
    and reports 830135 as an annual fee - a real bug this code shipped with,
    caught only because eight fixtures all returned suspiciously similar
    six-digit "fees" in the 830xxx range.

    A fee needs a fee label IN THE SAME TABLE. A number near the word "fee"
    somewhere on the page is not evidence of anything.
    """
    for row in table[:12]:
        for cell in row:
            # Deliberately NOT fee_total_column here: its bare "total" alias
            # would qualify a campus-area table as a fee table.
            if matches_label(cell, "fee_annual_inr") or matches_label(
                cell, "fee_total_row"
            ):
                return True
    return False


def is_result_table(table: list[list[str]]) -> bool:
    """A board-result summary masquerading as a strength table.

    [VERIFIED M2-2] "NO. OF REGISTERED STUDENTS | NO. OF STUDENTS PASSED |
    PASS PERCENTAGE" matches a class-wise-strength label but is a completely
    different number. Reading it as a headcount would inflate PCM-12 volume.
    """
    return any(
        matches_label(cell, "result_table_header") for row in table[:4] for cell in row
    )


def instalment_tables(tables: list[list[list[str]]]) -> list[list[list[str]]]:
    """Fee tables that publish ONE instalment each, not the annual total."""
    return [
        t
        for t in tables
        if is_fee_table(t)
        and any(
            matches_label(cell, "instalment_header") for row in t[:3] for cell in row
        )
    ]


def sum_instalments(
    tables: list[list[list[str]]],
) -> tuple[int | None, str | None]:
    """Add up one class's instalments across separate tables.

    Only sums rows whose class label matches across every instalment table. If
    the tables disagree about which classes exist, the sum would be guesswork,
    so this returns None and lets the field stay unknown (hard rule 6).
    """
    parts = instalment_tables(tables)
    if len(parts) < 2:
        return None, None

    per_class: dict[str, list[tuple[int, str]]] = {}
    for table in parts:
        for row in table:
            if not row:
                continue
            key = squash(row[0])
            if not key or key.isdigit():
                continue
            values = [
                n for cell in row[1:] for n in _numbers(cell) if 1_000 <= n <= 5_000_000
            ]
            if values:
                per_class.setdefault(key, []).append(
                    (max(values), " ".join(c for c in row if c))
                )

    complete = {
        key: values for key, values in per_class.items() if len(values) == len(parts)
    }
    if not complete:
        return None, None

    class12 = {k: v for k, v in complete.items() if matches_label(k, "class_12_row")}
    chosen = class12 or complete
    key, values = max(chosen.items(), key=lambda kv: sum(v for v, _ in kv[1]))
    total = sum(value for value, _ in values)
    evidence = " + ".join(f"{value}" for value, _ in values) + f" = {total} [{key}]"
    return total, evidence


def fee_from_tables(tables: list[list[list[str]]]) -> tuple[int | None, str | None]:
    """The annual fee for the highest relevant class, and its evidence row.

    Only ever reads from a table that is about fees. Prefers a class-11/12 row
    over the largest row total, because "12 PCMC 74080" is the exact figure this
    ICP needs rather than a school-wide maximum.
    """
    # Instalments split across separate tables must be summed, not picked from.
    summed, summed_evidence = sum_instalments(tables)
    if summed is not None:
        return summed, summed_evidence

    best: tuple[int, str] | None = None
    class12: tuple[int, str] | None = None

    for table in tables:
        if not is_fee_table(table) or is_result_table(table):
            continue

        # A TOTAL column is only ever declared in a header, so look for it in
        # the first row alone. Scanning every row until one matched meant a
        # "Total Fees | 182000" ROW was consumed as a header and its figure
        # thrown away - the parser then reported the tuition line instead,
        # understating a real school's fee by 36% and crossing a band.
        total_index = total_column_index(table[0]) if table else None

        # A row LABELLED as the total is the most valuable row in the table,
        # not something to skip. It wins outright over per-component rows.
        total_row = total_labelled_row(table)
        if total_row is not None:
            value, evidence = total_row
            if best is None or value > best[0]:
                best = (value, evidence)

        for index, row in enumerate(table):
            if not row or (index == 0 and total_index is not None):
                continue
            if is_not_money_row(row):
                continue

            joined = " ".join(c for c in row if c)
            if total_index is not None and total_index < len(row):
                candidates = _numbers(row[total_index])
            else:
                candidates = [n for cell in row for n in _numbers(cell)]
            plausible = [n for n in candidates if 1_000 <= n <= 5_000_000]
            if not plausible:
                continue

            value = max(plausible)
            is_class12 = any(matches_label(cell, "class_12_row") for cell in row)
            if is_class12 and (class12 is None or value > class12[0]):
                class12 = (value, joined)
            if best is None or value > best[0]:
                best = (value, joined)

    chosen = class12 or best
    return (chosen[0], chosen[1]) if chosen else (None, None)


def fee_year_from_text(text: str) -> int | None:
    """An academic year like 2024-25 or 2024-2025. A bare year is ambiguous."""
    # No leading \b: OCR squashes "ACADEMIC YEAR 2024-25" into
    # "ACADEMICYEAR2024-25", leaving no word boundary before the year.
    m = re.search(r"(20\d{2})\s*[-/]\s*(?:20)?\d{2}\b", text)
    return int(m.group(1)) if m else None


def labelled_value(
    tables: list[list[list[str]]], field: str
) -> tuple[str | None, str | None]:
    """The cell NEXT TO a matching label. The proforma is numbered two- and
    three-column rows, so the value is the last non-empty cell in the row."""
    for table in tables:
        for row in table:
            if not row or len(row) < 2:
                continue
            # Skip the leading serial number when matching.
            cells = [c for c in row if c]
            if not cells:
                continue
            label_cell = (
                cells[1]
                if cells[0].strip().rstrip(".").isdigit() and len(cells) > 2
                else cells[0]
            )
            if not matches_label(label_cell, field):
                continue
            value = cells[-1].strip()
            if value and value != label_cell:
                return value, " ".join(cells)[:400]
    return None, None


def labelled_int(
    tables: list[list[list[str]]], field: str, low: int = 1, high: int = 100_000
) -> tuple[int | None, str | None]:
    raw, evidence = labelled_value(tables, field)
    if raw is None:
        return None, None
    for number in _numbers(raw):
        if low <= number <= high:
            return number, evidence
    return None, None


def pincode_from(
    tables: list[list[list[str]]], text_blob: str
) -> tuple[str | None, str | None]:
    """A 6-digit Indian PIN, taken ONLY from PIN context.

    [VERIFIED M5] a page-wide scan for any 6-digit number returned 800013 and
    830887 - CBSE AFFILIATION numbers, which are also six digits and also start
    with 8. That is the fourth time a six-digit number that is not what we
    wanted has slipped through this parser (affiliation number as a fee, campus
    area as a fee, URL year as a fee, and now affiliation number as a PIN), so
    this one requires the word PIN nearby rather than trusting shape.
    """
    raw, evidence = labelled_value(tables, "pincode")
    if raw:
        match = re.search(r"\b([1-9]\d{5})\b", raw)
        if match:
            return match.group(1), evidence

    # Fall back to text, but only where "PIN" actually appears beside it.
    near_pin = re.search(
        r"pin\s*(?:code)?\s*[:\-]?\s*([1-9]\d{5})\b", text_blob or "", re.IGNORECASE
    )
    if near_pin:
        return near_pin.group(1), _window(text_blob, near_pin.group(1))
    return None, None


def estimate_enrollment(teachers: int | None) -> tuple[int | None, str | None]:
    """Approximate total students from published teacher count.

    The proforma publishes teachers far more often than students. This is an
    approximation WITH A STATED BASIS, and the caller marks it estimated so it
    renders as "~N (estimated)" rather than as a figure the school published.
    """
    if teachers is None:
        return None, None
    cfg = student_estimation()
    if not cfg["min_plausible_teachers"] <= teachers <= cfg["max_plausible_teachers"]:
        return None, None
    ratio = cfg["students_per_teacher"]
    return (
        teachers * ratio,
        f"estimated: {teachers} teachers x {ratio} students/teacher",
    )


# ---------------------------------------------------------------------------
# Strength
# ---------------------------------------------------------------------------


def class_12_total_from_tables(
    tables: list[list[list[str]]],
) -> tuple[int | None, str | None]:
    """Class-12 headcount from a class-wise strength table.

    If only a class-12 total is published with no stream split, record that and
    NOT pcm_12_count. Estimation is scoring's job, with a visible flag.
    """
    for table in tables:
        # Same scoping rule as fees: the table has to be about student numbers,
        # or a class-12 row in a fee table would be read as a headcount.
        if is_result_table(table):
            continue
        if not any(
            matches_label(cell, "class_wise_strength")
            for row in table[:12]
            for cell in row
        ):
            continue
        for row in table:
            if not row or not any(matches_label(cell, "class_12_row") for cell in row):
                continue
            counts = [n for cell in row for n in _numbers(cell) if 0 < n <= 5_000]
            if counts:
                return max(counts), " ".join(c for c in row if c)
    return None, None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def extract(body: bytes, *, is_fee_document: bool = False) -> MPDExtraction:
    """Tier 1 over one stored document. Emits nothing when nothing matches."""
    if body[:5] == b"%PDF-":
        tables, text = pdf_rows(body)
    else:
        tables, text = html_rows(body.decode("utf-8", "replace"))

    blocked = detect_student_data(tables)
    if blocked:
        return blocked_for_student_data(blocked)

    if is_scanned_pdf(body, text):
        # The document exists and is unreadable. Distinct from "no fee found":
        # different problem, different fix, and OCR (ADR-016) is the only route.
        return MPDExtraction(unreadable_fee_document=True)

    values: dict[str, object] = {}
    evidence: dict[str, str] = {}

    fee, fee_evidence = fee_from_tables(tables)
    if fee is not None:
        # The table header established the period; parse_inr would otherwise
        # refuse a bare number, which is correct everywhere else.
        values["fee_annual_inr_max"] = fee
        evidence["fee_annual_inr_max"] = fee_evidence[:500]
        year = fee_year_from_text(text)
        if year is not None:
            values["fee_year"] = year
            evidence["fee_year"] = _window(text, str(year))

    class_12, class_12_evidence = class_12_total_from_tables(tables)
    if class_12 is not None:
        values["class_12_total"] = class_12
        evidence["class_12_total"] = class_12_evidence[:500]

    teachers, teachers_evidence = labelled_int(tables, "total_teachers", 1, 2000)
    if teachers is not None:
        values["total_teachers"] = teachers
        evidence["total_teachers"] = teachers_evidence

    enrollment, enrollment_evidence = labelled_int(tables, "total_enrollment", 1, 20000)
    if enrollment is not None:
        values["total_enrollment"] = enrollment
        evidence["total_enrollment"] = enrollment_evidence
    else:
        estimated, basis = estimate_enrollment(teachers)
        if estimated is not None:
            values["total_enrollment"] = estimated
            values["enrollment_is_estimated"] = True
            evidence["total_enrollment"] = f"{teachers_evidence} | {basis}"

    pin, pin_evidence = pincode_from(tables, text)
    if pin is not None:
        values["pincode"] = pin
        evidence["pincode"] = pin_evidence

    counsellor, counsellor_evidence = labelled_value(tables, "counsellor_name")
    if counsellor and len(counsellor) < 80 and not counsellor[0].isdigit():
        values["counsellor_name"] = counsellor
        evidence["counsellor_name"] = counsellor_evidence

    if not is_fee_document:
        for field, value, span in _contacts(tables, text):
            values[field] = value
            evidence[field] = span

    return MPDExtraction(**values, evidence=evidence)


def _contacts(tables: list[list[list[str]]], text: str):
    """email / phone / principal, by label then by pattern within the row."""
    from school_intel.extract.mpd import patterns

    for row in (row for table in tables for row in table):
        joined = " ".join(c for c in row if c)
        for field in ("email", "phone", "principal_name"):
            if not any(matches_label(cell, field) for cell in row):
                continue
            value = patterns.value_for(field, joined)
            if value:
                yield field, value, joined[:500]
                break


def _window(text: str, needle: str, width: int = 80) -> str:
    index = text.find(needle)
    if index < 0:
        return needle
    return re.sub(r"\s+", " ", text[max(0, index - width) : index + width]).strip()


__all__ = [
    "EXTRACTOR",
    "detect_student_data",
    "extract",
    "fee_from_tables",
    "html_rows",
    "is_fee_table",
    "is_not_money_row",
    "is_result_table",
    "is_scanned_pdf",
    "labels",
    "matches_label",
    "normalize_person",
    "parse_inr",
    "pdf_rows",
    "squash",
    "strip_noise",
    "sum_instalments",
    "total_labelled_row",
]
