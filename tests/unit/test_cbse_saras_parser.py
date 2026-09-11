"""M1-1: the SARAS parsers, against saved real fixtures. No network."""

from pathlib import Path

import pytest

from school_intel.extract.parsers.cbse_saras import (
    SENIOR_SECONDARY,
    parse_detail,
    parse_list,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "cbse_saras"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def karnataka():
    return parse_list(fixture("list_state_KA.html"))


@pytest.fixture(scope="module")
def closed():
    return parse_list(fixture("list_ID1_D.html"))


def test_state_wise_row_count_reconciles_with_the_page(karnataka):
    """M1-1 requires failing loudly when the row count disagrees with the page's
    own stated total. Assert they agree on real HTML."""
    assert len(karnataka.rows) == 1847
    assert karnataka.seen == karnataka.stated_total


def test_stated_total_ignores_the_national_banner(karnataka):
    """[VERIFIED M0-0] The result count's LABEL is wrong - an affiliated query
    renders "Total No Of DisAffiliated Schools : 1847" because SARAS reuses one
    partial. 33147 is the national banner and must not be read as this result."""
    assert karnataka.stated_total == 1847
    assert karnataka.stated_total != 33147


def test_hard_gate_filters_to_roughly_a_third(karnataka):
    """[VERIFIED M0-0] ~29% are Senior Secondary, not ~66%. If this ratio moves a
    long way, the level filter or the source has changed."""
    senior = [r for r in karnataka.rows if r.has_class_12]
    assert len(senior) == 539
    assert all(r.status == SENIOR_SECONDARY for r in senior)
    assert 0.2 < len(senior) / len(karnataka.rows) < 0.45


def test_state_wise_list_carries_principal_address_and_website(karnataka):
    """This is why detail fetches are enrichment, not the primary parse."""
    senior = [r for r in karnataka.rows if r.has_class_12]
    assert sum(1 for r in senior if r.website) == 534
    assert all(r.principal_name for r in senior)
    row = next(r for r in senior if r.affiliation_no == "800001")
    assert row.name == "PM SHRI KENDRIYA VIDYALAYA ASC CENTRE"
    assert row.principal_name == "DR. NUTAN PUNJ"
    assert row.state == "KARNATAKA"
    assert row.district == "BENGALURU URBAN"
    assert row.website == "https://ascbangalore.kvs.ac.in"
    assert row.entity_key == "cbse:800001"


def test_fields_packed_in_one_cell_are_split_not_run_together(karnataka):
    """'Address : ... Website : ...' share a cell; the address must not swallow
    the website label."""
    row = next(r for r in karnataka.rows if r.affiliation_no == "800001")
    assert "Website" not in (row.address or "")
    assert row.address == "A.S.C.CENTRE SOUTH VICTORIA ROAD BANGALORE KARNATAKA"


def test_missing_website_is_none_not_empty_string(karnataka):
    senior = [r for r in karnataka.rows if r.has_class_12]
    blanks = [r for r in senior if not r.website]
    assert len(blanks) == 5
    assert all(r.website is None for r in blanks)


def test_closed_list_parses_and_counts_its_junk_row(closed):
    """[VERIFIED M0-0] The live closed list contains
    '400 | XXXXXXX | ... | TEST SCHOOL DEMO'. A non-numeric registration number
    cannot form an entity_key, so it is skipped and COUNTED - never guessed at,
    and never silently dropped, or the count check would fail spuriously."""
    assert len(closed.rows) == 490
    assert len(closed.skipped) == 1
    assert closed.seen == closed.stated_total == 491
    assert "TEST SCHOOL DEMO" in closed.skipped[0][1]


def test_closed_list_uses_a_different_column_order(closed):
    """Positional indexing across both layouts would be a bug: this list has
    State and District in their own columns, the state-wise one packs them."""
    row = closed.rows[0]
    assert row.affiliation_no == "130078"
    assert row.state == "ANDHRA PRADESH"
    assert row.affiliation_status == "Disaffiliated"
    assert row.region == "Vijaywada"


def test_empty_page_yields_no_rows_and_does_not_raise():
    page = parse_list("<html><body><table></table></body></html>")
    assert page.rows == []
    assert page.stated_total is None


def test_malformed_page_yields_no_rows_and_does_not_raise():
    page = parse_list("<html><body><tr><td>garbage</td></tr><p>not a table")
    assert page.rows == []


# --- detail page ---------------------------------------------------------


@pytest.fixture(scope="module")
def detail():
    return parse_detail(fixture("detail_330801.html"))


def test_detail_extracts_every_documented_field(detail):
    assert detail.values["name"] == "SUTARA MEHI MISSION SCHOOL"
    assert detail.values["state"] == "BIHAR"
    assert detail.values["district"] == "KATIHAR"
    assert detail.values["pincode"] == "854101"
    assert detail.values["website"] == "www.smmskursela.com"
    assert detail.values["year_founded"] == "2010"
    assert detail.values["principal_name"] == "SATYAJIT"
    assert (
        detail.values["legal_entity_name"] == "Sutara Social Empowerment Mission Trust"
    )
    assert detail.values["residential"] == "INDEPENDENT"


def test_detail_maps_school_status_to_the_hard_gate(detail):
    assert detail.values["has_class_12"] == "true"
    assert detail.values["grade_high"] == SENIOR_SECONDARY


def test_detail_splits_the_affiliation_period(detail):
    assert detail.values["board_valid_from"] == "01/04/2022"
    assert detail.values["board_valid_to"] == "31/03/2027"


def test_every_extracted_field_carries_evidence(detail):
    """An extraction without evidence is a bug, at every tier - it breaks
    /institutions/{id}/why, which must not care which tier produced a value."""
    assert set(detail.values) == set(detail.evidence)
    assert all(v.strip() for v in detail.evidence.values())
    assert "SUTARA MEHI MISSION SCHOOL" in detail.evidence["name"]


def test_principal_qualifications_are_never_stored(detail):
    """COMPLIANCE.md purpose limitation: no decision value, unnecessary personal
    data. The page publishes 'M A BED' and we must not keep it."""
    assert "M A BED" not in str(detail.values)
    assert not any("qualification" in k for k in detail.values)


def test_gender_is_not_mapped_to_a_school_field(detail):
    """[VERIFIED M0-0] This field is the PRINCIPAL's gender, not the school's.
    It sits directly under the principal name and reads like a school field."""
    assert "gender" not in detail.values


def test_detail_with_labels_absent_emits_nothing_rather_than_blanks():
    """A missing label is a normal outcome (hard rule 6)."""
    html = """
    <table>
      <tr><td>Name of Institution</td><td>PARTIAL SCHOOL</td></tr>
      <tr><td>State</td><td></td></tr>
      <tr><td>Unknown Label</td><td>something</td></tr>
    </table>
    """
    parsed = parse_detail(html)
    assert parsed.values == {"name": "PARTIAL SCHOOL"}
    assert "state" not in parsed.values
    assert "has_class_12" not in parsed.values
