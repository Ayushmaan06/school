"""M1-7: PIN -> metro and PIN -> centroid. Pure functions, no database."""

import csv
from pathlib import Path

import pytest

from school_intel.sources.pin_centroids import load_centroids, metro_for_pin


@pytest.mark.parametrize(
    "pincode,expected",
    [
        ("560037", "Bengaluru"),  # Marathahalli
        ("560001", "Bengaluru"),
        ("562125", "Bengaluru"),  # Bengaluru Rural
        ("110001", "Delhi NCR"),  # Delhi
        ("400001", "MMR (Mumbai)"),
        ("411001", "Pune"),
        ("500001", "Hyderabad"),
        ("600001", "Chennai"),
        ("700001", "Kolkata"),
        ("641001", "Coimbatore"),
        ("570001", "Mysuru"),
        ("854101", None),  # Katihar, Bihar - correctly not a metro
        ("999999", None),
    ],
)
def test_metro_lookup(pincode, expected):
    assert metro_for_pin(pincode) == expected


def test_delhi_ncr_spans_three_states():
    """The entire reason metro_area exists as a first-class field (6.2).
    Do not 'fix' this by splitting it per state."""
    delhi = metro_for_pin("110001")
    gurugram = metro_for_pin("122001")  # Haryana
    noida = metro_for_pin("201301")  # Uttar Pradesh
    assert delhi == gurugram == noida == "Delhi NCR"


def test_the_rest_of_haryana_and_up_are_not_ncr():
    """Longest-prefix matching is what makes the NCR carve-out precise rather
    than swallowing two whole states."""
    assert metro_for_pin("125001") is None  # Hisar, Haryana
    assert metro_for_pin("221001") is None  # Varanasi, UP


def test_chandigarh_tricity_spans_three_states():
    assert metro_for_pin("160017") == "Chandigarh"  # Chandigarh UT
    assert metro_for_pin("140301") == "Chandigarh"  # Mohali, Punjab
    assert metro_for_pin("134109") == "Chandigarh"  # Panchkula, Haryana


@pytest.mark.parametrize("bad", [None, "", "56003", "5600377", "ABCDEF", "56 00 37 1"])
def test_a_malformed_pin_returns_none_rather_than_guessing(bad):
    assert metro_for_pin(bad) is None


def test_a_pin_with_no_metro_is_none_not_an_error():
    """Most of India is not a metro. NULL scores in the lowest geography band,
    which is correct behaviour rather than a gap."""
    assert metro_for_pin("781001") is None  # Guwahati


def test_prefix_conflicts_are_rejected_at_load_time(monkeypatch, tmp_path):
    """A prefix claimed by two metros is a config bug that must fail loudly,
    not resolve to whichever happened to load last."""
    import school_intel.sources.pin_centroids as mod

    bad = tmp_path / "metro_areas.yaml"
    bad.write_text('Bengaluru:\n  - "560"\nChennai:\n  - "560"\n', encoding="utf-8")
    monkeypatch.setattr(mod, "METRO_CONFIG", bad)
    mod._metro_index.cache_clear()
    try:
        with pytest.raises(ValueError, match="claimed by both"):
            mod.metro_for_pin("560001")
    finally:
        mod._metro_index.cache_clear()


# --- the India Post CSV --------------------------------------------------


def _write_csv(path: Path, rows: list[dict], header: list[str]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_load_centroids_reads_a_normal_file(tmp_path):
    path = _write_csv(
        tmp_path / "pins.csv",
        [
            {
                "Pincode": "560037",
                "Latitude": "12.9560",
                "Longitude": "77.7010",
                "District": "Bangalore",
                "StateName": "KARNATAKA",
            }
        ],
        ["Pincode", "Latitude", "Longitude", "District", "StateName"],
    )
    got = load_centroids(path)
    assert len(got) == 1
    assert got[0].pincode == "560037"
    assert got[0].state == "KARNATAKA"


def test_column_names_are_matched_by_alias_not_position(tmp_path):
    """Published copies of this dataset disagree about column names and order."""
    path = _write_csv(
        tmp_path / "alt.csv",
        [{"lat": "12.95", "long": "77.70", "pin_code": "560037", "state_name": "KA"}],
        ["lat", "long", "pin_code", "state_name"],
    )
    got = load_centroids(path)
    assert len(got) == 1
    assert got[0].pincode == "560037"


def test_missing_required_columns_fail_loudly(tmp_path):
    path = _write_csv(tmp_path / "bad.csv", [{"Pincode": "560037"}], ["Pincode"])
    with pytest.raises(ValueError, match="missing required column"):
        load_centroids(path)


def test_rows_without_usable_coordinates_are_skipped_not_defaulted(tmp_path):
    """(0, 0) is the Gulf of Guinea, not a plausible school location."""
    path = _write_csv(
        tmp_path / "mixed.csv",
        [
            {"Pincode": "560037", "Latitude": "12.95", "Longitude": "77.70"},
            {"Pincode": "560038", "Latitude": "", "Longitude": ""},
            {"Pincode": "560039", "Latitude": "0", "Longitude": "0"},
            {"Pincode": "560040", "Latitude": "abc", "Longitude": "def"},
            {"Pincode": "12", "Latitude": "12.95", "Longitude": "77.70"},
        ],
        ["Pincode", "Latitude", "Longitude"],
    )
    got = load_centroids(path)
    assert [c.pincode for c in got] == ["560037"]
