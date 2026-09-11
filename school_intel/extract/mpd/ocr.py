"""Tier 1b - OCR for scanned fee documents. ADR-016.

[VERIFIED M0-0] ~30% of resolved fee documents are scanned images with no text
layer. pdfplumber returns 0-1 characters, and no label parser, regex or TEXT LLM
reads an image - the gap is an input-modality problem, not an extraction-quality
one. Without OCR, fee coverage caps near 30%.

Local and deterministic: `rapidocr-onnxruntime` installs from PyPI with no system
binary and no API key, so `uv sync` still runs the whole pipeline and
`extract --reextract` still makes zero fetches and zero API calls. ADR-013 is not
weakened.

**The wrong-value guard is the load-bearing part of this module.** A digit misread
moves an institution across affordability bands - 74080 -> 7408 is four bands -
and hard rule 6 says a missing value beats a wrong one. So an OCR'd fee is
flagged, carries reduced confidence, must parse cleanly, and goes to review when
it lands near a band edge.
"""

import io
import logging
import re
from functools import lru_cache
from pathlib import Path

import yaml

from school_intel.extract.mpd import tables
from school_intel.extract.mpd.schemas import MPDExtraction

log = logging.getLogger(__name__)

EXTRACTOR = "ocr:rapidocr_v1"
SCORING_PATH = Path(__file__).parents[2] / "score" / "scoring.yaml"

# Rendering resolution. 200 dpi was enough to read the M0-0 sample exactly;
# higher costs time and lower starts dropping thin digits.
RENDER_DPI = 200
MAX_PAGES = 4


@lru_cache(maxsize=1)
def _config() -> dict:
    scoring = yaml.safe_load(SCORING_PATH.read_text("utf-8"))
    return scoring["ocr"]


@lru_cache(maxsize=1)
def band_edges() -> tuple[int, ...]:
    """Affordability band boundaries, from scoring.yaml. Hard rule 8: these are
    never written into Python."""
    scoring = yaml.safe_load(SCORING_PATH.read_text("utf-8"))
    return tuple(
        band["upto"]
        for band in scoring["affordability_bands"]
        if band["upto"] is not None
    )


def near_band_boundary(value: int) -> bool:
    """Within the configured margin of an affordability band edge.

    This is exactly where one wrong digit changes the ranking, so these go to
    review rather than straight into a score.
    """
    margin = _config()["band_boundary_margin"]
    return any(abs(value - edge) <= edge * margin for edge in band_edges())


def is_plausible_fee(value: int) -> bool:
    cfg = _config()
    return cfg["min_plausible_fee_inr"] <= value <= cfg["max_plausible_fee_inr"]


def observation_confidence() -> float:
    return float(_config()["observation_confidence"])


# ---------------------------------------------------------------------------
# Reading the image
# ---------------------------------------------------------------------------


def render_pages(body: bytes, max_pages: int = MAX_PAGES) -> list:
    """PDF pages as PIL images. Returns [] if the PDF cannot be rendered."""
    import pdfplumber

    images = []
    try:
        with pdfplumber.open(io.BytesIO(body)) as doc:
            for page in doc.pages[:max_pages]:
                images.append(page.to_image(resolution=RENDER_DPI).original)
    except Exception as exc:  # noqa: BLE001 - an unrenderable PDF is data
        log.info("could not render PDF for OCR: %s", exc)
    return images


# Two text boxes whose vertical centres are within this fraction of the page
# height belong to the same table row.
ROW_TOLERANCE = 0.012


def group_into_rows(
    boxes: list[tuple[float, float, str]], page_height: float
) -> list[str]:
    """Reassemble OCR text boxes into table ROWS.

    The engine returns one box per CELL, not per row, so a naive "one box = one
    line" reading separates "XITOXII" from the figures beside it and finds a
    class-12 label with no numbers on it. Clustering by vertical centre and then
    sorting by x rebuilds the row the human eye sees.
    """
    if not boxes:
        return []
    tolerance = max(page_height * ROW_TOLERANCE, 1.0)
    rows: list[list[tuple[float, float, str]]] = []
    for box in sorted(boxes, key=lambda b: b[1]):
        if rows and abs(box[1] - rows[-1][0][1]) <= tolerance:
            rows[-1].append(box)
        else:
            rows.append([box])
    return [" ".join(t for _, _, t in sorted(row, key=lambda b: b[0])) for row in rows]


def ocr_lines(body: bytes) -> list[str]:
    """Reconstructed table rows from a scanned document, in reading order."""
    try:
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:  # pragma: no cover - declared in pyproject
        log.warning("rapidocr not installed; skipping OCR")
        return []

    engine = _engine(RapidOCR)
    lines: list[str] = []
    for image in render_pages(body):
        array = np.array(image.convert("RGB"))
        try:
            result, _ = engine(array)
        except Exception as exc:  # noqa: BLE001 - a bad page is data
            log.info("OCR failed on a page: %s", exc)
            continue
        boxes = []
        for entry in result or []:
            polygon, text = entry[0], entry[1].strip()
            if not text:
                continue
            xs = [float(point[0]) for point in polygon]
            ys = [float(point[1]) for point in polygon]
            boxes.append((min(xs), sum(ys) / len(ys), text))
        lines.extend(group_into_rows(boxes, page_height=float(array.shape[0])))
    return lines


@lru_cache(maxsize=1)
def _engine(factory):
    """One engine per process - constructing it loads ONNX models."""
    return factory()


# ---------------------------------------------------------------------------
# Turning OCR'd lines into a fee
# ---------------------------------------------------------------------------

_NUMBER = re.compile(r"\d[\d,]*")


def fee_from_lines(lines: list[str]) -> tuple[int | None, str | None]:
    """The class-11/12 fee from OCR'd text, or nothing.

    Label matching goes through `tables.squash`, which removes ALL whitespace:
    [VERIFIED M0-0] OCR emits "XITOXII" and "MAY2024" with the spaces dropped,
    so a space-sensitive comparison misses every label on a scanned document.

    Takes the LARGEST figure on a class-12 line, because fees are published as
    instalments and the total is what matters. Reading the first number would
    report one instalment as the annual fee.
    """
    best: tuple[int, str] | None = None
    for line in lines:
        if not tables.matches_label(line, "class_12_row"):
            continue
        values = [_as_int(raw) for raw in _NUMBER.findall(line)]
        plausible = [v for v in values if v is not None and is_plausible_fee(v)]
        if not plausible:
            continue
        value = max(plausible)
        if best is None or value > best[0]:
            best = (value, line)
    return best if best else (None, None)


def _as_int(raw: str) -> int | None:
    """Clean integer or nothing. No repair heuristics, no nearest plausible
    value - a repaired digit is a guessed digit."""
    cleaned = raw.replace(",", "")
    return int(cleaned) if cleaned.isdigit() else None


def extract(body: bytes) -> MPDExtraction:
    """OCR one scanned fee document. Only ever called by the caller that already
    established this is a fee document with no text layer."""
    lines = ocr_lines(body)
    if not lines:
        return MPDExtraction(unreadable_fee_document=True)

    fee, evidence = fee_from_lines(lines)
    if fee is None:
        return MPDExtraction(unreadable_fee_document=True)

    joined = " ".join(lines)
    year = tables.fee_year_from_text(joined)
    values: dict[str, object] = {"fee_annual_inr_max": fee}
    spans = {"fee_annual_inr_max": evidence[:500]}
    if year is not None:
        values["fee_year"] = year
        spans["fee_year"] = evidence[:500]

    # Deliberately NOT extracted from OCR: principal_name, email, phone.
    # [VERIFIED M0-0] OCR renders "Principal" as "Pingipa!" while getting the
    # tabular digits exactly right. Trust the numeric grid, never the prose.
    return MPDExtraction(**values, evidence=spans)


def needs_review(extraction: MPDExtraction) -> str | None:
    """Why this OCR'd fee should go to review_queue, or None.

    ADR-016 point 4: a value within the configured margin of a band edge is
    exactly where one misread digit changes the ranking.
    """
    fee = extraction.fee_annual_inr_max
    if fee is None:
        return None
    if near_band_boundary(fee):
        return (
            f"ocr fee {fee} is within "
            f"{int(_config()['band_boundary_margin'] * 100)}% of an affordability "
            "band boundary; one misread digit would change the band"
        )
    return None
