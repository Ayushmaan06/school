"""ADR-015. Per-field precision and recall against hand-verified ground truth.

Without this, "improve the parser" is unfalsifiable and every later change is a
guess. It got MORE important under ADR-013, not less: deterministic coverage is
an unknown to be measured, and this harness is what decides whether tier 3 ever
gets switched on. It is also how a parser version is compared against an OCR
version over identical stored documents.

Definitions, stated because they are easy to conflate:

    coverage   = of the institutions in gold, how many did we produce a value for
    precision  = of the values we produced, how many were right
    recall     = of the values gold has, how many did we get right

Missing-in-gold and missing-in-system are distinguished throughout. A field gold
does not know about is not a system failure, and counting it as one makes the
number meaningless in the direction that flatters us.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

RESULTS_DIR = Path("tests/eval/results")

# Fee comparison uses a tolerance band, not equality: a school publishes several
# near-identical figures (with and without a component, across two instalment
# schemes), so ±5% is agreement rather than error.
FEE_TOLERANCE = 0.05
TOLERANT_FIELDS = {"fee_annual_inr"}


@dataclass
class FieldScore:
    field: str
    gold_count: int = 0
    produced: int = 0
    correct: int = 0
    wrong: int = 0
    missing_in_system: int = 0

    @property
    def precision(self) -> float | None:
        return round(self.correct / self.produced, 3) if self.produced else None

    @property
    def recall(self) -> float | None:
        return round(self.correct / self.gold_count, 3) if self.gold_count else None

    @property
    def coverage(self) -> float | None:
        return round(self.produced / self.gold_count, 3) if self.gold_count else None


@dataclass
class EvalReport:
    run_date: str
    extractor: str
    fields: dict[str, dict] = field(default_factory=dict)
    gold_entities: int = 0
    #: Gold rows whose institution is not in the corpus at all. Reported
    #: separately - a school we never imported is a coverage question, not an
    #: extraction failure.
    gold_without_institution: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def values_agree(field_name: str, expected, actual) -> bool:
    """Exact match, except where the doc says a tolerance band applies."""
    if expected is None or actual is None:
        return False
    if field_name in TOLERANT_FIELDS:
        try:
            expected_number = float(str(expected).replace(",", ""))
            actual_number = float(str(actual).replace(",", ""))
        except (TypeError, ValueError):
            return False
        if expected_number == 0:
            return actual_number == 0
        return (
            abs(actual_number - expected_number) / abs(expected_number) <= FEE_TOLERANCE
        )
    return str(expected).strip().lower() == str(actual).strip().lower()


def load_gold(session: Session) -> list[tuple[str, str, object]]:
    rows = session.execute(
        text(
            "SELECT institution_key, field, expected_json FROM eval_gold"
            " ORDER BY institution_key, field"
        )
    ).all()
    return [(key, field_name, _unwrap(value)) for key, field_name, value in rows]


def _unwrap(value):
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def canonical_values(session: Session, keys: set[str]) -> dict[tuple[str, str], object]:
    """What the system currently believes, keyed by (entity_key, field).

    Read from `observations` rather than `institutions`, so the eval measures
    EXTRACTION rather than resolution. A field the resolver dropped for a policy
    reason is a different bug from a field the parser never found.
    """
    if not keys:
        return {}
    rows = session.execute(
        text(
            "SELECT DISTINCT ON (entity_key, field) entity_key, field, value_text"
            " FROM observations"
            " WHERE entity_key = ANY(:keys) AND superseded_by IS NULL"
            " ORDER BY entity_key, field, confidence DESC, observed_at DESC"
        ),
        {"keys": sorted(keys)},
    ).all()
    return {(key, field_name): value for key, field_name, value in rows}


def run(session: Session, extractor: str = "all") -> EvalReport:
    gold = load_gold(session)
    keys = {key for key, _, _ in gold}
    actual = canonical_values(session, keys)

    known_keys = set(
        session.execute(
            text(
                "SELECT DISTINCT entity_key FROM observations WHERE entity_key = ANY(:k)"
            ),
            {"k": sorted(keys)},
        )
        .scalars()
        .all()
    )

    scores: dict[str, FieldScore] = {}
    for key, field_name, expected in gold:
        score = scores.setdefault(field_name, FieldScore(field=field_name))
        score.gold_count += 1
        produced = actual.get((key, field_name))
        if produced is None:
            score.missing_in_system += 1
            continue
        score.produced += 1
        if values_agree(field_name, expected, produced):
            score.correct += 1
        else:
            score.wrong += 1

    report = EvalReport(
        run_date=datetime.now(UTC).date().isoformat(),
        extractor=extractor,
        gold_entities=len(keys),
        gold_without_institution=len(keys - known_keys),
    )
    for name, score in sorted(scores.items()):
        report.fields[name] = {
            **asdict(score),
            "precision": score.precision,
            "recall": score.recall,
            "coverage": score.coverage,
        }
    return report


def write_report(report: EvalReport, directory: Path = RESULTS_DIR) -> Path:
    """Committed, so extraction quality becomes a tracked time series."""
    directory.mkdir(parents=True, exist_ok=True)
    safe = report.extractor.replace(":", "_").replace("/", "_")
    path = directory / f"{report.run_date}-{safe}.json"
    path.write_text(report.to_json(), encoding="utf-8")
    return path


def previous_report(
    report: EvalReport, directory: Path = RESULTS_DIR
) -> EvalReport | None:
    """The most recent earlier run for this extractor, for a delta."""
    safe = report.extractor.replace(":", "_").replace("/", "_")
    candidates = sorted(directory.glob(f"*-{safe}.json"))
    candidates = [p for p in candidates if p.stem != f"{report.run_date}-{safe}"]
    if not candidates:
        return None
    data = json.loads(candidates[-1].read_text(encoding="utf-8"))
    return EvalReport(**data)


def format_table(report: EvalReport, previous: EvalReport | None = None) -> str:
    """The per-field table `make eval` prints.

    A prompt or model change that does not report its eval delta is not
    reviewable (ADR-015), so the delta column is not optional.
    """
    header = (
        f"eval {report.run_date}  extractor={report.extractor}  "
        f"gold entities={report.gold_entities}"
    )
    lines = [header]
    if report.gold_without_institution:
        lines.append(
            f"  ({report.gold_without_institution} gold entities are not in the "
            "corpus at all - a coverage question, not an extraction failure)"
        )
    lines.append("")
    lines.append(
        f"{'field':22} {'gold':>5} {'produced':>9} {'correct':>8} "
        f"{'precision':>10} {'recall':>8} {'delta':>8}"
    )
    lines.append("-" * 76)
    for name, values in report.fields.items():
        delta = ""
        if previous and name in previous.fields:
            before = previous.fields[name].get("recall")
            after = values.get("recall")
            if before is not None and after is not None:
                delta = f"{after - before:+.3f}"
        lines.append(
            f"{name:22} {values['gold_count']:>5} {values['produced']:>9} "
            f"{values['correct']:>8} {_fmt(values['precision']):>10} "
            f"{_fmt(values['recall']):>8} {delta:>8}"
        )
    if not report.fields:
        lines.append("(eval_gold is empty - nothing to measure)")
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"
