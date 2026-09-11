"""M2-2b: OCR for scanned fee documents (ADR-016).

Runs against the REAL scanned PDF that motivated the ADR - vvi.edu.in's fee
structure, 0 characters of text layer.
"""

from pathlib import Path

import pytest

from school_intel.extract.mpd import ocr, tables
from school_intel.extract.mpd.schemas import MPDExtraction

FX = Path(__file__).parent.parent / "fixtures" / "mpd"
SCANNED = FX / "fee_scanned_vvi.pdf"
TEXT_PDF = FX / "fee_text_lakemont.pdf"


@pytest.fixture(scope="module")
def scanned_lines():
    return ocr.ocr_lines(SCANNED.read_bytes())


@pytest.fixture(scope="module")
def scanned_result():
    return ocr.extract(SCANNED.read_bytes())


# --- the ADR-016 validation case ----------------------------------------


def test_a_scanned_fee_table_is_read(scanned_result):
    """0 characters of text layer, yet the annual class-11/12 fee comes out
    exactly. Without OCR this document contributes nothing."""
    assert scanned_result.fee_annual_inr_max == 138600


def test_the_total_is_taken_not_the_first_instalment(scanned_result):
    """The row is "55500 27700 27700 27700 138600". Reading the first figure
    reports one instalment as the annual fee - a different band."""
    assert scanned_result.fee_annual_inr_max != 55500
    assert scanned_result.fee_annual_inr_max != 27700


def test_evidence_is_the_ocrd_row(scanned_result):
    evidence = scanned_result.evidence["fee_annual_inr_max"]
    assert "138600" in evidence
    assert "XITOXII" in evidence.replace(" ", "")


# --- the wrong-value guard, the load-bearing part ------------------------


def test_this_fee_lands_near_a_band_edge_and_goes_to_review(scanned_result):
    """138600 is within 10% of the 150000 boundary. One misread digit moves the
    institution a whole affordability band, so it must not go straight into a
    score."""
    reason = ocr.needs_review(scanned_result)
    assert reason is not None
    assert "band boundary" in reason


def test_a_fee_far_from_any_edge_does_not_need_review():
    assert not ocr.near_band_boundary(220000)
    assert ocr.near_band_boundary(148000)
    assert ocr.near_band_boundary(52000)


def test_band_edges_come_from_scoring_yaml_not_python():
    """Hard rule 8: never hardcode a scoring band in Python."""
    assert ocr.band_edges() == (50000, 100000, 150000, 300000, 600000, 1000000)


def test_ocr_observations_carry_reduced_confidence():
    """So they lose to any text-layer or HTML fee in resolve/conflict.py."""
    assert 0 < ocr.observation_confidence() < 1.0


def test_a_garbled_number_emits_nothing_rather_than_a_repair():
    """No repair heuristics and no nearest-plausible-value: a repaired digit is
    a guessed digit, and hard rule 6 says missing beats wrong."""
    fee, _ = ocr.fee_from_lines(["XITOXII 7A08O l38.6OO"])
    assert fee is None


def test_an_implausible_figure_is_rejected():
    assert ocr.fee_from_lines(["XII 12"])[0] is None
    assert ocr.fee_from_lines(["XII 99999999"])[0] is None


def test_no_class_12_row_yields_nothing():
    fee, _ = ocr.fee_from_lines(["NUR LKG UKG 27000 81000", "ITOIV 123400"])
    assert fee is None


# --- what OCR must never be trusted for ---------------------------------


def test_principal_name_is_never_sourced_from_ocr(scanned_result):
    """[VERIFIED M0-0] OCR renders "Principal" as "Pingipa!" while getting the
    tabular digits exactly right. Trust the numeric grid, never the prose."""
    assert scanned_result.principal_name is None
    assert scanned_result.email is None
    assert scanned_result.phone is None


# --- row reconstruction --------------------------------------------------


def test_boxes_are_grouped_into_rows_not_read_one_per_line():
    """The engine returns one box per CELL. Reading them as lines separates a
    class label from the figures beside it, so the class-12 row appears to have
    no numbers on it at all."""
    boxes = [
        (10.0, 100.0, "XITOXII"),
        (200.0, 101.0, "55500"),
        (400.0, 99.0, "138600"),
        (10.0, 300.0, "ITOIV"),
        (200.0, 301.0, "80500"),
    ]
    rows = ocr.group_into_rows(boxes, page_height=2000.0)
    assert rows == ["XITOXII 55500 138600", "ITOIV 80500"]


def test_row_grouping_handles_an_empty_page():
    assert ocr.group_into_rows([], page_height=1000.0) == []


def test_the_real_scan_reconstructs_a_class_12_row(scanned_lines):
    matches = [
        line for line in scanned_lines if tables.matches_label(line, "class_12_row")
    ]
    assert matches, "no class-12 row was reconstructed from the scan"
    assert any("138600" in line for line in matches)


# --- routing -------------------------------------------------------------


def test_a_text_layer_pdf_never_invokes_ocr():
    """OCR is for documents with nothing to parse. Running it on a readable PDF
    would be slower and less accurate than the text layer."""
    body = TEXT_PDF.read_bytes()
    _, text = tables.pdf_rows(body)
    assert not tables.is_scanned_pdf(body, text)


def test_an_unreadable_scan_is_flagged_rather_than_silently_empty():
    result = ocr.extract(b"%PDF-1.4 not renderable")
    assert result.unreadable_fee_document is True
    assert isinstance(result, MPDExtraction)
