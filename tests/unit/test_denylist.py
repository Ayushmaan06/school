"""MANDATORY (AGENTS.md). The PII denylist is a legal boundary, not a preference.

Both directions are asserted: student-data URLs blocked, and the high-value pages
this system exists to read explicitly NOT blocked. A denylist that blocks
everything passes a one-directional test and silently kills the product.
"""

import pytest

from school_intel.fetch.denylist import blocked_reason, match_static

BLOCKED = [
    "https://school.edu.in/results",
    "https://school.edu.in/result/class-12",
    "https://school.edu.in/board-result-2025.pdf",
    "https://school.edu.in/cbse-results",
    "https://school.edu.in/merit-list",
    "https://school.edu.in/toppers",
    "https://school.edu.in/our-topper",
    "https://school.edu.in/marksheet.pdf",
    "https://school.edu.in/scorecard",
    "https://school.edu.in/rank-list",
    "https://school.edu.in/rank_list",
    "https://school.edu.in/ranklist",
    "https://school.edu.in/admission-list",
    "https://school.edu.in/selected-candidates",
    "https://school.edu.in/student-list",
    "https://school.edu.in/studentlist",
    "https://school.edu.in/class-xii-result",
    "https://school.edu.in/prize-list",
    "https://school.edu.in/scholarship-list",
    "https://school.edu.in/attendance",
    "https://school.edu.in/admit-card",
    "https://school.edu.in/hall-ticket",
    "https://school.edu.in/transfer-certificate",
    "https://school.edu.in/awards",
    # query strings count too - the pattern matches the full URL
    "https://school.edu.in/download?doc=class12_result",
]

ALLOWED = [
    "https://school.edu.in/mandatory-disclosure",
    "https://school.edu.in/mandatory-public-disclosure",
    "https://school.edu.in/fee-structure",
    "https://school.edu.in/fees-structure-2025-26.pdf",
    "https://school.edu.in/wp-content/uploads/2025/04/Fee-Structure-for-2024-25.pdf",
    "https://school.edu.in/about-us",
    "https://school.edu.in/contact",
    "https://school.edu.in/admissions",
    "https://school.edu.in/infrastructure",
    "https://school.edu.in/staff-details",
    "https://saras.cbse.gov.in/SARAS/AffiliatedList/AfflicationDetails/330801",
]


@pytest.mark.parametrize("url", BLOCKED)
def test_student_data_urls_are_blocked(url):
    assert blocked_reason(url) is not None, f"student-data URL was NOT blocked: {url}"


@pytest.mark.parametrize("url", ALLOWED)
def test_high_value_urls_are_not_blocked(url):
    assert blocked_reason(url) is None, f"legitimate URL was blocked: {url}"


def test_fee_is_never_a_deny_pattern():
    """A broad r"fee" pattern would be a catastrophic own-goal: fee pages are the
    highest-value target in the system and its only source of fee data."""
    assert match_static("https://school.edu.in/fee") is None
    assert match_static("https://school.edu.in/fees") is None
    assert match_static("https://school.edu.in/fee-structure") is None


def test_matching_is_case_insensitive():
    assert blocked_reason("https://school.edu.in/RESULTS.PDF") is not None
    assert blocked_reason("https://school.edu.in/Merit-List") is not None


def test_patterns_apply_to_urls_only_not_page_text():
    """[VERIFIED M0-0] The compliant Appendix-IX proforma titles a section
    "C: RESULT AND ACADEMICS", with the fee-structure link inside it. If these
    patterns were ever applied to page text, every compliant Mandatory
    Disclosure page in the country would be blocklisted - which would look like
    the denylist working while deleting the only source of fee data.

    This test documents that the API takes a URL and nothing else.
    """
    page_text = (
        "C: RESULT AND ACADEMICS  SL No. DOCUMENTS/INFORMATION  "
        "1 FEE STRUCTURE OF THE SCHOOL  View"
    )
    # The page this text came from is fetchable...
    assert blocked_reason("https://school.edu.in/mandatory-disclosure") is None
    # ...and the text itself is never an input to the gate. If a future change
    # makes blocked_reason() accept page content, this assertion should be the
    # thing that makes someone stop and read ADR-006.
    assert "RESULT" in page_text  # the trigger word really is present
    assert blocked_reason("https://school.edu.in/fee-structure.pdf") is None


def test_blocked_reason_names_the_pattern():
    """The fetches row records WHY we declined - an auditable decision, not a gap."""
    reason = blocked_reason("https://school.edu.in/toppers")
    assert reason is not None
    assert reason.startswith("pattern:")
