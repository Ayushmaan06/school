"""M2-1: MPD discovery. Pure functions - the async driver is tested separately.

Every branch the plan names, plus the M0-0 findings encoded as assertions.
"""

import pytest

from school_intel.sources.cbse_mpd import (
    MAX_FETCHES,
    candidate_paths,
    disclosure_links,
    fee_document_url,
    homepage_url,
    is_login_wall,
    looks_like_mpd,
    name_matches,
)

# The real Appendix-IX shape. [VERIFIED M0-0] on rcis.in and vvi.edu.in.
PROFORMA = """
<html><body>
<h3>C : RESULT AND ACADEMICS</h3>
<table>
  <tr><th>SL No.</th><th>DOCUMENTS/INFORMATION</th><th>LINKS OF UPLOADED DOCUMENTS</th></tr>
  <tr><td>1</td><td>FEE STRUCTURE OF THE SCHOOL</td>
      <td><a href="/uploads/2025/Fee-Structure-2024-25.pdf">View</a></td></tr>
  <tr><td>2</td><td>ANNUAL ACADEMIC CALENDER</td>
      <td><a href="/uploads/calendar.pdf">View</a></td></tr>
  <tr><td>3</td><td>LIST OF SCHOOL MANAGEMENT COMMITTEE (SMC)</td>
      <td><a href="/uploads/smc.pdf">View</a></td></tr>
</table>
<p>Affiliation No 800001. Trust: Green Valley Trust. Student strength 1200.</p>
</body></html>
"""


# --- homepage anchor harvesting -----------------------------------------


def test_anchor_matched_by_href():
    html = '<a href="/mandatory-public-disclosure/">Disclosures</a>'
    assert disclosure_links(html, "https://s.test") == [
        "https://s.test/mandatory-public-disclosure/"
    ]


def test_anchor_matched_by_text_wrapped_in_a_span():
    """The v1 probe matched only the raw text between > and <, which misses
    every site that wraps its nav labels in a <span>. That single bug halved the
    measured discovery rate."""
    html = '<a href="/about/x123"><span>Mandatory Disclosure</span></a>'
    assert disclosure_links(html, "https://s.test") == ["https://s.test/about/x123"]


def test_a_directly_linked_pdf_sorts_first():
    """[VERIFIED M0-0] Some schools publish the whole proforma as one PDF
    (/disclosure/b1.pdf) - the shortest path to the data."""
    html = (
        '<a href="/mandatory-disclosure/">Disclosure page</a>'
        '<a href="/disclosure/b1.pdf">Mandatory disclosure PDF</a>'
    )
    assert disclosure_links(html, "https://s.test")[0].endswith(".pdf")


def test_unrelated_anchors_are_ignored():
    html = '<a href="/admissions">Admissions</a><a href="/gallery">Photo Gallery</a>'
    assert disclosure_links(html, "https://s.test") == []


def test_relative_and_absolute_hrefs_both_resolve():
    html = (
        '<a href="mandatory-disclosure">rel</a>'
        '<a href="https://other.test/mandatory-disclosure">abs</a>'
    )
    got = disclosure_links(html, "https://s.test")
    assert "https://s.test/mandatory-disclosure" in got
    assert "https://other.test/mandatory-disclosure" in got


# --- recognising the index page -----------------------------------------


def test_a_real_proforma_is_recognised():
    assert looks_like_mpd(PROFORMA)


def test_a_marketing_homepage_is_not():
    assert not looks_like_mpd("Welcome to our school. Admissions open. Gallery.")


def test_a_login_wall_is_detected_and_not_mistaken_for_an_index():
    wall = "Parent Login  Username Password  Sign in to continue"
    assert is_login_wall(wall)
    assert not looks_like_mpd(wall)


def test_a_disclosure_page_that_merely_mentions_login_is_not_a_wall():
    assert not is_login_wall(PROFORMA + " Parent Login")


# --- the wrong-school guard ----------------------------------------------


def test_a_page_for_another_school_on_shared_hosting_is_rejected():
    page = "Welcome to Blue Ridge International Academy. Fee structure. Trust."
    assert not name_matches(page, "Green Valley Public School")


def test_a_page_for_the_right_school_passes_despite_word_order():
    page = "GREEN VALLEY PUBLIC SCHOOL, BENGALURU - Mandatory Disclosure"
    assert name_matches(page, "Green Valley School")


def test_an_unknown_canonical_name_does_not_block_discovery():
    assert name_matches("anything at all", "")


# --- the fee document, one hop deeper ------------------------------------


def test_fee_link_is_found_by_row_label_not_anchor_text():
    """[VERIFIED M0-0] The anchor text is "View" on every row. The ROW LABEL is
    what identifies the fee link, so a text-based match would pick row 1, 2 or 3
    at random."""
    got = fee_document_url(PROFORMA, "https://s.test/mandatory-disclosure")
    assert got == "https://s.test/uploads/2025/Fee-Structure-2024-25.pdf"


def test_the_calendar_and_smc_rows_are_not_mistaken_for_the_fee():
    got = fee_document_url(PROFORMA, "https://s.test/x")
    assert "calendar" not in got.lower()
    assert "smc" not in got.lower()


def test_fee_link_falls_back_to_a_plain_list_layout():
    html = '<ul><li><a href="/docs/fee-structure.pdf">Fee Structure</a></li></ul>'
    assert (
        fee_document_url(html, "https://s.test/x")
        == "https://s.test/docs/fee-structure.pdf"
    )


def test_no_fee_row_returns_none_rather_than_the_first_link():
    html = '<table><tr><td>ANNUAL CALENDAR</td><td><a href="/c.pdf">View</a></td></tr></table>'
    assert fee_document_url(html, "https://s.test/x") is None


def test_javascript_and_anchor_hrefs_are_skipped():
    html = (
        "<table><tr><td>FEE STRUCTURE OF THE SCHOOL</td>"
        '<td><a href="#">View</a><a href="/real-fee.pdf">View</a></td></tr></table>'
    )
    assert fee_document_url(html, "https://s.test/x") == "https://s.test/real-fee.pdf"


# --- the fetch budget ----------------------------------------------------


def test_guessed_paths_reserve_a_fetch_for_the_fee_document():
    """Reaching an index page with no budget left to follow its fee link is the
    worst outcome: it costs the fetches and yields no fee."""
    paths = candidate_paths("https://s.test", budget=MAX_FETCHES)
    assert len(paths) == MAX_FETCHES - 1


def test_an_exhausted_budget_yields_no_further_guesses():
    assert candidate_paths("https://s.test", budget=1) == []
    assert candidate_paths("https://s.test", budget=0) == []


# --- homepage url normalisation ------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("vvi.edu.in", "https://vvi.edu.in"),
        ("https://vvi.edu.in/", "https://vvi.edu.in"),
        ("http://vvi.edu.in", "http://vvi.edu.in"),
        ("  vvi.edu.in  ", "https://vvi.edu.in"),
        (None, None),
        ("", None),
    ],
)
def test_homepage_url(raw, expected):
    assert homepage_url(raw) == expected


# --- what a "principal" label must NOT yield ----------------------------


def test_a_qualification_row_is_not_a_principal_name():
    """[VERIFIED M2-4] The proforma row "PRINCIPAL QUALIFICATION | Msc, B.Ed"
    was extracted as a principal_name of "qualification msc". That is a wrong
    value, and storing a qualification at all breaks the COMPLIANCE.md purpose
    limitation - so this must return nothing, not a cleaned-up version."""
    from school_intel.extract.mpd.patterns import find_principal

    assert find_principal("6 PRINCIPAL QUALIFICATION Msc, B.Ed") is None
    assert find_principal("PRINCIPAL EXPERIENCE 22 years") is None
    assert find_principal("PRINCIPAL SIGNATURE") is None


def test_a_real_principal_row_still_yields_the_name():
    from school_intel.extract.mpd.patterns import find_principal

    assert find_principal("PRINCIPAL: Dr Nutan Punj") == "nutan punj"
    assert find_principal("Name of Principal / Head: Lokesh Bihari Sharma") is not None
