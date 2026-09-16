"""M3-1: the CISCE locator parser, against a saved real fixture. No network."""

from pathlib import Path

import pytest

from school_intel.extract.cisce import card_claims
from school_intel.extract.parsers.cisce import SchoolCard, parse_address, parse_result
from school_intel.fetch import denylist
from school_intel.resolve import conflict
from school_intel.sources.base import RowCountMismatchError
from school_intel.sources.cisce import check_row_count, result_url

FIXTURES = Path(__file__).parent.parent / "fixtures" / "cisce"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def page():
    return parse_result(fixture("locate_result_isc_india_p1.html"))


def test_every_card_on_the_page_parses(page):
    """Ten per page, fixed by the server. Nothing may be silently dropped."""
    assert len(page.rows) == 10
    assert page.skipped == []


def test_stated_total_is_the_whole_filtered_query(page):
    """[VERIFIED 2026-09-15] `Showing 1 to 10 of 1928 entries` - 1928 is the ISC
    total for India, not this page's ten. The importer checks it once per run."""
    assert page.stated_total == 1928


def test_isc_is_the_class_12_gate(page):
    """ICSE alone stops at class 10. The gate reads the ISC badge, never ICSE."""
    assert all(card.has_class_12 for card in page.rows)
    icse_only = SchoolCard(code="XX001", name="Some School", affiliations=("ICSE",))
    assert not icse_only.has_class_12


def test_the_list_carries_everything_there_is(page):
    """There is no detail page, so a miss here is a permanent miss."""
    first = page.rows[0]
    assert first.code == "AN001"
    assert first.entity_key == "cisce:AN001"
    assert first.name == "Don Bosco School"
    assert first.principal_name == "Fr. BHARATHRAJA C"
    assert first.state == "Andaman and Nicobar Islands"
    assert first.district == "South Andaman"
    assert first.pincode == "744103"
    assert first.gender == "Co-ed."
    assert first.residential == "Day"


def test_website_is_captured_when_present(page):
    """`website` seeds S2, which is the only route to phone, email and fee."""
    sites = {c.code: c.website for c in page.rows if c.website}
    assert sites["AP003"] == "https://www.loyolapublicschool.org"
    # Trailing whitespace inside the href must not reach the enrichment stage.
    assert sites["AP010"] == "https://www.nasrschool.in"


def test_state_is_never_derived_from_the_code_prefix(page):
    """The AP0xx block predates the 2014 split and is full of Telangana schools.
    A prefix-to-state shortcut would misfile them and skew geography scoring."""
    ap_coded = [c for c in page.rows if c.code.startswith("AP")]
    assert any(c.state == "Telangana" for c in ap_coded)


@pytest.mark.parametrize(
    "blob, expected",
    [
        # The tail is fixed; the left-hand side is not. Parse from the right.
        (
            "PORT BLAIR, South Andaman, Andaman and Nicobar Islands, 744103, India",
            ("PORT BLAIR", "South Andaman", "Andaman and Nicobar Islands", "744103"),
        ),
        # Pincodes appear with an internal space. Normalise it out.
        (
            "Amalapuram, East Godavari, Andhra Pradesh, 533 201, India",
            ("Amalapuram", "East Godavari", "Andhra Pradesh", "533201"),
        ),
        # Four locality segments, not one. A positional parse loses three of them.
        (
            "12 Main, Indiranagar, Bengaluru Urban, Karnataka, 560038, India",
            ("12 Main, Indiranagar", "Bengaluru Urban", "Karnataka", "560038"),
        ),
        # Missing pincode: leave it null rather than shifting state into it.
        ("Sector 5, Gurgaon, Haryana, India", ("Sector 5", "Gurgaon", "Haryana", None)),
    ],
)
def test_address_is_parsed_from_the_right(blob, expected):
    parts = parse_address(blob)
    assert (
        parts["address"],
        parts["district"],
        parts["state"],
        parts["pincode"],
    ) == expected


def test_blank_fields_emit_no_claim():
    """Hard rule 6: missing beats wrong. An empty card yields only what it has."""
    claims = card_claims(SchoolCard(code="XX001", name="Some School"))
    assert {c.field for c in claims} == {"name"}


def test_claims_carry_the_gate_and_its_evidence(page):
    claims = {c.field: c for c in card_claims(page.rows[0])}
    assert claims["has_class_12"].value == "true"
    assert claims["has_class_12"].evidence == "Affiliation: ICSE, ISC"
    assert claims["grade_high"].value == "12"


def test_row_count_mismatch_fails_loudly():
    """A silent shortfall looks exactly like "CISCE lost 400 schools this quarter"."""
    check_row_count(1928, 1928, "India/ISC")
    with pytest.raises(RowCountMismatchError):
        check_row_count(1900, 1928, "India/ISC")


def test_result_url_requests_the_gate_server_side():
    url = result_url(7)
    assert "affiliation=ISC" in url
    assert "country=India" in url
    assert url.endswith("page=7")


def test_every_field_cisce_emits_ranks_cisce(page):
    """A source missing from a field's FIELD_PRIORITY tuple has its observations
    DROPPED, not ranked lower. The first live import hit exactly that: 1,928
    institutions resolved with the right board and a null address, pincode,
    state, website and principal, because the table still assumed CBSE was the
    only registry. The next importer must fail here instead of in production."""
    emitted = {claim.field for card in page.rows for claim in card_claims(card)}
    assert emitted, "fixture produced no claims"
    missing = {
        field
        for field in emitted
        if "cisce" not in conflict.FIELD_PRIORITY.get(field, ())
    }
    assert not missing, f"cisce emits {missing} but does not rank for them"


def test_the_import_url_survives_the_pii_denylist():
    """The locator's own form posts to /result, and `result` is an ADR-006 deny
    pattern - the one that stops us ever fetching a student result page. The
    root route serves identical data, so we use it and leave hard rule 1 alone.
    A regression here means the whole import silently fetches nothing."""
    assert denylist.blocked_reason(result_url(1)) is None
    assert denylist.match_static("https://locate.cisce.org/result?page=1") is not None
