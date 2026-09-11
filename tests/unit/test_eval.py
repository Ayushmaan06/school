"""M2-4: the eval harness (ADR-015). Pure scoring logic - no database."""

import json

import pytest

from school_intel.eval import EvalReport, FieldScore, format_table, values_agree

# --- comparison ----------------------------------------------------------


@pytest.mark.parametrize(
    "expected,actual,agree",
    [
        (185000, 185000, True),
        (185000, 190000, True),  # +2.7%, inside the tolerance band
        (185000, 194000, True),  # +4.9%
        (185000, 195000, False),  # +5.4%, outside
        (185000, 18500, False),  # a 10x error must never read as agreement
        (185000, None, False),
    ],
)
def test_fee_uses_a_tolerance_band_not_equality(expected, actual, agree):
    """A school publishes several near-identical figures across instalment
    schemes, so +-5% is agreement. A 10x error is not."""
    assert values_agree("fee_annual_inr", expected, actual) is agree


def test_non_numeric_fields_compare_exactly_but_forgive_case():
    assert values_agree("principal_name", "A B Sharma", "a b sharma")
    assert not values_agree("principal_name", "A B Sharma", "A B Verma")


def test_a_missing_value_never_counts_as_agreement():
    assert not values_agree("fee_annual_inr", None, 185000)
    assert not values_agree("fee_annual_inr", 185000, None)


# --- the three numbers, which are easy to conflate -----------------------


def test_precision_recall_and_coverage_are_distinct():
    score = FieldScore(field="fee_annual_inr", gold_count=10)
    score.produced = 4
    score.correct = 3
    score.wrong = 1
    score.missing_in_system = 6

    assert score.precision == 0.75, "of what we produced, how much was right"
    assert score.recall == 0.3, "of what gold has, how much did we get right"
    assert score.coverage == 0.4, "of gold, how much did we produce anything for"


def test_missing_in_system_is_not_counted_as_wrong():
    """Silence and error are different failures with different fixes. Counting
    a miss as an error would make precision meaningless."""
    score = FieldScore(field="fee_annual_inr", gold_count=10)
    score.produced = 2
    score.correct = 2
    score.missing_in_system = 8
    assert score.precision == 1.0
    assert score.recall == 0.2


def test_an_empty_gold_set_yields_none_not_zero():
    """0.0 reads as "we score zero"; None reads as "there is nothing to
    measure". Reporting the first would be a lie in our favour."""
    score = FieldScore(field="fee_annual_inr")
    assert score.precision is None
    assert score.recall is None
    assert score.coverage is None


# --- the report ----------------------------------------------------------


def _report(recall: float, extractor: str = "all") -> EvalReport:
    return EvalReport(
        run_date="2026-09-05",
        extractor=extractor,
        gold_entities=3,
        fields={
            "fee_annual_inr": {
                "field": "fee_annual_inr",
                "gold_count": 10,
                "produced": 5,
                "correct": int(recall * 10),
                "wrong": 0,
                "missing_in_system": 5,
                "precision": 1.0,
                "recall": recall,
                "coverage": 0.5,
            }
        },
    )


def test_the_table_shows_a_delta_against_the_previous_run():
    """A parser or model change that does not report its eval delta is not
    reviewable (ADR-015), so the delta column is not optional."""
    table = format_table(_report(0.5), previous=_report(0.3))
    assert "+0.200" in table


def test_a_regression_shows_a_negative_delta():
    table = format_table(_report(0.3), previous=_report(0.5))
    assert "-0.200" in table


def test_the_table_works_with_no_previous_run():
    assert "fee_annual_inr" in format_table(_report(0.5), previous=None)


def test_an_empty_gold_set_says_so_rather_than_printing_nothing():
    empty = EvalReport(run_date="2026-09-05", extractor="all")
    assert "eval_gold is empty" in format_table(empty)


def test_gold_entities_missing_from_the_corpus_are_reported_separately():
    """A school we never imported is a coverage question, not an extraction
    failure, and must not be blamed on the parser."""
    report = _report(0.5)
    report.gold_without_institution = 2
    assert "not in the corpus" in format_table(report)


def test_a_report_round_trips_through_json():
    report = _report(0.5)
    assert EvalReport(**json.loads(report.to_json())).fields == report.fields
