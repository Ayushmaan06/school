"""MANDATORY (AGENTS.md). Table-driven, covering every case in
docs/ENTITY-RESOLUTION.md "Testing requirements".

No I/O, no database, no network in the module under test.
"""

import pytest

from school_intel.extract.normalize import (
    match_key,
    normalize_city,
    normalize_domain,
    normalize_legal_entity,
    normalize_person,
    normalize_phone,
    normalize_title,
    parse_inr,
)

# --- the cases the doc names explicitly ----------------------------------

SAME_KEY = [
    ("DPS Bangalore", "Delhi Public School, Bengaluru"),
    ("St. Mary's High School", "Saint Marys School"),
    ("Sri Chaitanya Jr College", "Sri Chaitanya Junior College"),
    ("K V ASC Centre", "Kendriya Vidyalaya ASC Centre"),
    ("ABC Sr. Sec. School", "ABC Senior Secondary School"),
    ("XYZ Intl School", "XYZ International School"),
    ("Green Valley Public School", "Green Valley School"),
    # token order must not matter
    ("Bengaluru Delhi Public School", "Delhi Public School Bengaluru"),
]


@pytest.mark.parametrize("left,right", SAME_KEY)
def test_names_that_must_share_a_match_key(left, right):
    assert match_key(left) == match_key(right), f"{left!r} != {right!r}"


DIFFERENT_KEY = [
    ("Delhi Public School Bengaluru", "Delhi Public School Mysuru"),
    ("St Marys School", "St Josephs School"),
    ("ABC School North", "ABC School South"),
]


@pytest.mark.parametrize("left,right", DIFFERENT_KEY)
def test_names_that_must_not_share_a_match_key(left, right):
    """A false merge silently deletes a lead, so over-matching is the expensive
    direction here."""
    assert match_key(left) != match_key(right)


def test_match_key_never_returns_empty_for_an_all_generic_name():
    """An empty key would match every other empty key - a merge machine."""
    assert match_key("The Public School") != ""
    assert match_key("The Public School") != match_key("The High School")


def test_match_key_is_not_the_display_name():
    """canonical_name is for humans; match_key is derived and never shown."""
    assert match_key("Delhi Public School") != "Delhi Public School"


# --- cities ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Gurgaon", "gurugram"),
        ("Gurugram", "gurugram"),
        ("Bangalore", "bengaluru"),
        ("BANGALORE URBAN", "bengaluru urban"),
        ("Mysore", "mysuru"),
        ("Calcutta", "kolkata"),
        ("Bombay", "mumbai"),
        ("Trivandrum", "thiruvananthapuram"),
        ("Chennai", "chennai"),
    ],
)
def test_city_renames(raw, expected):
    assert normalize_city(raw) == expected


def test_gurgaon_and_gurugram_are_the_same_city():
    assert normalize_city("Gurgaon") == normalize_city("Gurugram")


def test_missing_city_is_none():
    assert normalize_city(None) is None
    assert normalize_city("") is None


# --- people and titles ----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Dr. A.B. Sharma", "a b sharma"),
        ("A B Sharma", "a b sharma"),
        ("Mrs. Nutan Punj", "nutan punj"),
        ("SHRI RAJESH KUMAR", "rajesh kumar"),
        ("Prof.  Meera   Iyer ", "meera iyer"),
    ],
)
def test_person_normalisation(raw, expected):
    assert normalize_person(raw) == expected


def test_honorific_changes_do_not_look_like_a_new_principal():
    """M4-3 must not emit principal_change when a source merely adds a title.
    It compares normalize_person output, never raw strings."""
    assert normalize_person("Dr. A B Sharma") == normalize_person("A B Sharma")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Principal", "principal"),
        ("HeadMaster", "principal"),
        ("Head of Institution", "principal"),
        ("Vice Principal", "vice_principal"),
        ("Deputy Principal", "vice_principal"),
        ("Career Counsellor", "career_counsellor"),
        ("Career Counselor", "career_counsellor"),
        ("Academic Coordinator", "academic_coordinator"),
        ("Managing Director", "director"),
        ("Chairman", "trustee"),
        ("Correspondent", "trustee"),
        ("Sports Teacher", "other"),
        (None, "other"),
    ],
)
def test_title_normalisation(raw, expected):
    assert normalize_title(raw) == expected


def test_vice_principal_does_not_collapse_to_principal():
    """Longest-alias-first matters: 'vice principal' contains 'principal'."""
    assert normalize_title("Vice Principal") == "vice_principal"


# --- domains and phones ---------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://www.vvi.edu.in/", "vvi.edu.in"),
        ("http://vvi.edu.in", "vvi.edu.in"),
        ("www.VVI.edu.in/mandatory-disclosure", "vvi.edu.in"),
        ("VVI.EDU.IN", "vvi.edu.in"),
        ("https://school.edu.in:8080/x?y=1", "school.edu.in"),
        # [VERIFIED M0-0] real garbage from the SARAS registry itself
        ("https://.agragamividyakendra.com/", "agragamividyakendra.com"),
        ("http://www.tosss.edu.in.", "tosss.edu.in"),
        ("https://a..b.com", None),
        ("NA", None),
        ("", None),
        (None, None),
    ],
)
def test_domain_normalisation(raw, expected):
    assert normalize_domain(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+91 80 2345 6789", ["8023456789"]),
        ("080-23456789", ["8023456789"]),
        ("9483338135", ["9483338135"]),
        ("Ph: 8050005851 / 9483338135", ["8050005851", "9483338135"]),
        ("", []),
        (None, []),
    ],
)
def test_phone_normalisation(raw, expected):
    assert normalize_phone(raw) == expected


def test_the_same_number_written_two_ways_collapses():
    assert normalize_phone("+91-80-23456789") == normalize_phone("080 2345 6789")


# --- legal entity (the ADR-005 group axis) -------------------------------


def test_legal_suffix_variation_does_not_split_one_trust():
    a = normalize_legal_entity("Sutara Social Empowerment Mission Trust")
    b = normalize_legal_entity("Sutara Social Empowerment Mission Trust (Regd.)")
    assert a == b and a is not None


def test_different_trusts_stay_different():
    assert normalize_legal_entity("Sutara Mission Trust") != normalize_legal_entity(
        "Vagdevi Vilas Trust"
    )


# --- money. The function that must return None. --------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Rs. 1.85 lakh per annum", 185000),
        ("1,85,000 per annum", 185000),
        ("INR 185000 p.a.", 185000),
        ("1.85 L per year", 185000),
        ("Rs 1,38,600 annually", 138600),
        ("Rs 15,000 per month", 180000),
        ("Rs 15,000 / month", 180000),
        ("Rs 15,000 pm", 180000),
        ("Rs 45,000 per quarter", 180000),
    ],
)
def test_fee_parsing_when_the_period_is_explicit(raw, expected):
    assert parse_inr(raw) == expected


AMBIGUOUS = [
    "Rs 15,000",
    "1,85,000",
    "INR 185000",
    "Rs. 1.85 lakh",
    "Fee: 25000",
    "Rs 20,000 per term",
    "Rs 15,000 per month per annum",
]


@pytest.mark.parametrize("raw", AMBIGUOUS)
def test_ambiguous_period_returns_none_and_never_guesses(raw):
    """The most important assertion in this file.

    A 12x error moves an institution across four affordability bands, so an
    ambiguous period must record nothing. Missing beats wrong (hard rule 6).
    """
    assert parse_inr(raw) is None, f"{raw!r} was guessed at instead of skipped"


def test_per_term_is_none_because_a_term_is_not_a_fixed_period():
    """Some schools run three terms, some two. Unknowable from the string."""
    assert parse_inr("Rs 20,000 per term") is None


def test_contradictory_periods_return_none():
    assert parse_inr("Rs 15,000 per month per annum") is None


def test_assume_annual_is_opt_in_only():
    """For contexts where the document already established the period, e.g. an
    Appendix-IX column header reading ANNUAL FEE."""
    assert parse_inr("1,85,000") is None
    assert parse_inr("1,85,000", assume_annual=True) == 185000


@pytest.mark.parametrize(
    "raw", ["Rs 50 per annum", "Rs 9,00,00,000 per annum", "abc", ""]
)
def test_implausible_or_unparseable_values_return_none(raw):
    assert parse_inr(raw) is None


def test_none_input_is_none():
    assert parse_inr(None) is None


def test_the_verified_ocr_sample_parses():
    """[VERIFIED M0-0] 'XI TO XII ... TOTAL 138600' from a real scanned fee PDF.
    The caller supplies the period from the table header."""
    assert parse_inr("138600", assume_annual=True) == 138600
