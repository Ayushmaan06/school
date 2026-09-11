"""M2-2: tier 1 extraction, against 15 REAL checked-in fixtures. No network.

The plan requires at least 12 fixtures spanning tabular HTML, free-layout HTML,
text PDF, scanned PDF and a non-conforming page. These are the actual documents
M0-0 found, not synthetic ones.
"""

from pathlib import Path

import pytest

from school_intel.extract.mpd import tables
from school_intel.extract.mpd.schemas import NON_VALUE_FIELDS, MPDExtraction

FX = Path(__file__).parent.parent / "fixtures" / "mpd"
INDEXES = sorted(FX.glob("index_*.html"))
FEE_DOCS = sorted(FX.glob("fee_*.pdf"))


def load(name: str) -> bytes:
    return (FX / name).read_bytes()


def extract(name: str) -> MPDExtraction:
    return tables.extract(load(name), is_fee_document=name.startswith("fee_"))


# --- the bug that shipped ------------------------------------------------


@pytest.mark.parametrize("path", INDEXES, ids=lambda p: p.name)
def test_an_index_page_never_reports_a_fee(path):
    """THE regression test for this module.

    The first cut took the largest plausible number in the document, which on
    the Appendix-IX proforma is the CBSE AFFILIATION NUMBER. Eight fixtures all
    returned "fees" in the 830xxx range - 830135, 830184, 830115 - and every one
    of them was an affiliation number presented as an annual fee.

    An index page carries a LINK to the fee document, never the figure, so the
    only correct answer here is None.
    """
    result = tables.extract(path.read_bytes())
    assert result.fee_annual_inr_max is None, (
        f"{path.name} reported {result.fee_annual_inr_max} as a fee; "
        "check it is not the affiliation number"
    )


def test_a_fee_needs_a_fee_label_in_the_same_table():
    affiliation = [
        [["Affiliation Number", "830135"], ["Year of Establishment", "2005"]]
    ]
    fee = [[["Class", "Fee Structure"], ["XII", "185000"]]]
    assert tables.fee_from_tables(affiliation) == (None, None)
    assert tables.fee_from_tables(fee)[0] == 185000


# --- fee documents -------------------------------------------------------


def test_grade_and_stream_table_yields_the_class_12_row():
    """[VERIFIED M0-0] "12 PCMB 74510" is the exact figure this ICP needs,
    rather than a school-wide maximum."""
    result = extract("fee_text_lakemont.pdf")
    assert result.fee_annual_inr_max == 74510
    assert "PCMB" in result.evidence["fee_annual_inr_max"]


def test_instalments_split_across_tables_are_summed():
    """[VERIFIED M2-2] newhorizongurukul.in publishes "1st Installment Fees" and
    "2nd Installment Fees" as two tables. Taking either alone understates the
    annual fee by about half, which crosses affordability bands."""
    result = extract("fee_newhorizon.pdf")
    assert result.fee_annual_inr_max == 174900
    evidence = result.evidence["fee_annual_inr_max"]
    assert "79900 + 95000" in evidence, "the arithmetic must be visible in evidence"
    assert "science" in evidence.lower()


def test_a_single_instalment_is_never_reported_as_the_annual_fee():
    result = extract("fee_newhorizon.pdf")
    assert result.fee_annual_inr_max not in (79900, 95000)


def test_sum_needs_matching_classes_in_every_instalment_table():
    """If the tables disagree about which classes exist, the sum is guesswork."""
    mismatched = [
        [["1st Installment Fees", ""], ["XII", "50000"]],
        [["2nd Installment Fees", ""], ["IX", "40000"]],
    ]
    assert tables.sum_instalments(mismatched) == (None, None)


def test_a_scanned_pdf_is_flagged_not_reported_as_missing(caplog):
    """ADR-016. The document exists and is unreadable - a different problem from
    "no fee found", with a different fix, and it routes to review rather than
    looking like a discovery miss."""
    result = extract("fee_scanned_vvi.pdf")
    assert result.unreadable_fee_document is True
    assert result.fee_annual_inr_max is None
    assert not result.has_fee


def test_a_text_pdf_is_not_flagged_as_scanned():
    assert extract("fee_text_lakemont.pdf").unreadable_fee_document is False


# --- evidence ------------------------------------------------------------


@pytest.mark.parametrize("path", INDEXES + FEE_DOCS, ids=lambda p: p.name)
def test_every_populated_field_carries_evidence(path):
    """Enforced by the schema itself, at every tier. This asserts the real
    fixtures actually satisfy it rather than trusting the validator."""
    result = tables.extract(
        path.read_bytes(), is_fee_document=path.name.startswith("fee_")
    )
    # The SAME set the validator uses, so the two cannot drift.
    tracked = set(MPDExtraction.model_fields) - NON_VALUE_FIELDS
    for name in tracked:
        if getattr(result, name) is not None:
            assert name in result.evidence
            assert result.evidence[name].strip()


def test_schema_rejects_a_value_without_evidence():
    with pytest.raises(ValueError, match="without evidence"):
        MPDExtraction(fee_annual_inr_max=185000)


# --- the student-data gate ----------------------------------------------


def test_a_result_table_yields_zero_observations():
    """Structural: a name column beside a marks column. On a hit the caller
    deletes the raw document and adds the URL to denylist_learned."""
    result_table = [
        [
            ["S.No", "Student Name", "Roll No", "Marks", "Percentage"],
            ["1", "A Sharma", "12345", "478", "95.6"],
        ]
    ]
    assert tables.detect_student_data(result_table) is not None


@pytest.mark.parametrize("path", INDEXES, ids=lambda p: p.name)
def test_real_proforma_pages_are_not_blocked_as_student_data(path):
    """The compliant proforma is titled "C: RESULT AND ACADEMICS". A text-level
    rule would blocklist every Mandatory Disclosure page in India, deleting the
    only source of fee data while appearing to work."""
    result = tables.extract(path.read_bytes())
    assert result.student_data_detected is False


def test_aggregate_pass_percentages_are_not_student_data():
    """Board-result SUMMARIES carry no names, so they are not PII - but they are
    also not strength, which is the separate trap below."""
    summary = [
        [
            ["Year", "Registered", "Passed", "Pass Percentage"],
            ["2024", "210", "210", "100%"],
        ]
    ]
    assert tables.detect_student_data(summary) is None


def test_a_result_summary_is_never_read_as_class_strength():
    """[VERIFIED M2-2] vvi.edu.in publishes "NO. OF REGISTERED STUDENTS | NO. OF
    STUDENTS PASSED | PASS PERCENTAGE", which matches a strength label but is a
    completely different number. Reading it would inflate PCM-12 volume."""
    summary = [
        [
            [
                "SL No.",
                "YEAR",
                "NO. OF REGISTERED STUDENTS",
                "NO. OF STUDENTS PASSED",
                "PASS PERCENTAGE",
            ],
            ["1", "2023-24", "210", "210", "100%"],
            ["2", "XII", "204", "204", "100%"],
        ]
    ]
    assert tables.is_result_table(summary[0])
    assert tables.class_12_total_from_tables(summary) == (None, None)


# --- label matching ------------------------------------------------------


def test_label_matching_ignores_whitespace_entirely():
    """[VERIFIED M0-0] OCR emits "XITOXII" and "MAY2024" with spaces dropped."""
    assert tables.squash("XI TO XII") == tables.squash("XITOXII")
    assert tables.matches_label("XITOXII", "class_12_row")
    assert tables.matches_label("Fee  Structure", "fee_annual_inr")
    assert tables.matches_label("FeeStructure", "fee_annual_inr")


def test_the_total_column_is_preferred_over_the_first_figure():
    """Fees are published as instalments across COLUMNS too.

    Note the "Fee Structure" caption. A table is only treated as a fee table if
    something in it carries FEE meaning - a bare "TOTAL" column is not enough,
    because "TOTAL CAMPUS AREA (IN SQ MTR)" also matches that. The trade-off is
    deliberate: this misses a fee grid that never says "fee" anywhere, and in
    exchange it never reports a campus area as an annual fee. Missing beats
    wrong (hard rule 6).
    """
    table = [
        ["Fee Structure", "", "", "", ""],
        ["CLASS", "MAY", "AUGUST", "NOVEMBER", "TOTAL"],
        ["XI TO XII", "55500", "27700", "27700", "138600"],
    ]
    assert tables.total_column_index(table[1]) == 4
    assert tables.fee_from_tables([table])[0] == 138600


def test_a_class_amount_grid_with_no_fee_word_is_deliberately_missed():
    """Documents the cost of the rule above, so it is a choice rather than a
    surprise. If this starts mattering on real documents, widen labels.yaml -
    that is a config edit, not a code change."""
    unlabelled = [
        [
            ["CLASS", "MAY", "TOTAL"],
            ["XI TO XII", "55500", "138600"],
        ]
    ]
    assert tables.fee_from_tables(unlabelled) == (None, None)


def test_no_match_is_a_normal_outcome_not_an_error():
    empty = tables.extract(b"<html><body><p>Welcome to our school</p></body></html>")
    assert empty.is_empty
    assert empty.fee_annual_inr_max is None
    assert empty.student_data_detected is False


def test_a_malformed_pdf_is_data_not_a_crash():
    result = tables.extract(b"%PDF-1.4 this is not really a pdf")
    assert result.fee_annual_inr_max is None


# --- the total row, and what "total" must not mean ----------------------


def test_a_total_labelled_row_beats_its_component_rows():
    """[VERIFIED M2-4] A component breakdown lists tuition, food and
    miscellaneous as separate rows and then publishes a total. Taking the
    largest component reported Rs 116,525 where the school published
    Rs 182,000 - understating a real fee by 36% and crossing a band."""
    breakdown = [
        [
            ["Particulars", "Sl. No.", "26-27"],
            ["Annual Tuition Fee", "1", "116525"],
            ["Food Charges", "2", "48150"],
            ["Miscellaneous Expenses", "3", "17325"],
            ["Total Fees", "182000"],
        ]
    ]
    value, evidence = tables.fee_from_tables(breakdown)
    assert value == 182000
    assert "Total Fees" in evidence


def test_a_total_row_is_not_consumed_as_a_header():
    """The bug behind the above: total-column detection scanned every row until
    one matched, so the "Total Fees" ROW was treated as a header and skipped."""
    table = [
        ["Particulars", "Amount"],
        ["Tuition", "100000"],
        ["Total Fees", "150000"],
    ]
    assert tables.total_labelled_row(table) == (150000, "Total Fees 150000")
    assert tables.fee_from_tables([table])[0] == 150000


def test_bare_total_does_not_make_something_a_fee():
    """[VERIFIED M2-4] "TOTAL CAMPUS AREA OF THE SCHOOL (IN SQ MTR) 19070" was
    reported as an annual fee. A label must carry FEE meaning, not just the word
    total - the same class of bug as reading the affiliation number."""
    campus = [
        [
            ["1", "TOTAL CAMPUS AREA OF THE SCHOOL (IN SQ MTR)", "19070 Sq.Mts."],
            ["2", "NO. AND SIZE OF THE CLASS ROOMS", "45"],
        ]
    ]
    assert not tables.is_fee_table(campus[0])
    assert tables.total_labelled_row(campus[0]) is None
    assert tables.fee_from_tables(campus) == (None, None)


def test_a_total_column_header_still_works_inside_a_fee_table():
    """Bare "total" remains valid as a COLUMN header, because the surrounding
    table is already established as being about fees."""
    table = [
        ["CLASS", "MAY", "AUGUST", "TOTAL"],
        ["Fee Structure", "", "", ""],
        ["XI TO XII", "55500", "27700", "138600"],
    ]
    assert tables.total_column_index(table[0]) == 3
    assert tables.fee_from_tables([table])[0] == 138600
