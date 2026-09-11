"""The schema EVERY tier writes to, so tiers are interchangeable and comparable.

Tier 1 (label/table), tier 2 (patterns), tier 1b (OCR, ADR-016) and tier 3 (LLM,
off by default) all produce an `MPDExtraction`. That is what lets `make eval`
compare a parser version against an OCR version against a model version over
identical stored documents (ADR-015).

Every field is Optional. `evidence` is not.
"""

from pydantic import BaseModel, Field, model_validator

#: Fields that are metadata rather than extracted values, so they need no
#: evidence. Exported so tests assert against the SAME set the validator uses -
#: two copies of this list drift, and the drift shows up as a confusing
#: "populated without evidence" failure on a boolean.
NON_VALUE_FIELDS = frozenset(
    {
        "evidence",
        "student_data_detected",
        "unreadable_fee_document",
        "enrollment_is_estimated",
    }
)


class MPDExtraction(BaseModel):
    """One document's worth of claims about one institution."""

    fee_annual_inr_min: int | None = None
    fee_annual_inr_max: int | None = None
    fee_year: int | None = None
    fee_component_breakdown: dict[str, int] | None = None

    class_12_total: int | None = None
    pcm_12_count: int | None = None
    pcm_12_count_year: int | None = None
    total_enrollment: int | None = None
    total_teachers: int | None = None
    pincode: str | None = None

    #: True when total_enrollment was derived from teacher count rather than
    #: published. Rendered as "~N (estimated)" so nobody mistakes it for a
    #: figure the school stated.
    enrollment_is_estimated: bool = False

    email: str | None = None
    phone: str | None = None
    principal_name: str | None = None
    counsellor_name: str | None = None
    counsellor_title: str | None = None
    streams: list[str] | None = None

    #: field name -> the verbatim source text supporting it. Mandatory for every
    #: populated field, at every tier. A parser records the matched row exactly
    #: as a model would record its span, because /institutions/{id}/why must not
    #: care which tier produced a value.
    evidence: dict[str, str] = Field(default_factory=dict)

    #: Set when the document turns out to hold student records. The caller
    #: deletes the raw document, adds the URL to denylist_learned, and emits
    #: nothing. See docs/COMPLIANCE.md.
    student_data_detected: bool = False

    #: A fee PDF with no text layer (ADR-016). Distinct from "no fee found":
    #: the document exists and is unreadable, which is a different problem with
    #: a different fix, and it routes to review_queue rather than looking like a
    #: discovery miss.
    unreadable_fee_document: bool = False

    @model_validator(mode="after")
    def _every_populated_field_has_evidence(self) -> "MPDExtraction":
        """An extraction without evidence is a bug at every tier."""
        if self.student_data_detected:
            return self
        tracked = set(type(self).model_fields) - NON_VALUE_FIELDS
        missing = [
            name
            for name in tracked
            if getattr(self, name) is not None and name not in self.evidence
        ]
        if missing:
            raise ValueError(
                f"populated without evidence: {sorted(missing)}. Every extracted "
                "field carries the verbatim matched source text (AGENTS.md)."
            )
        return self

    @property
    def has_fee(self) -> bool:
        return (
            self.fee_annual_inr_min is not None or self.fee_annual_inr_max is not None
        )

    @property
    def is_empty(self) -> bool:
        """No match is a NORMAL outcome, not an error."""
        return not self.evidence and not self.student_data_detected


def blocked_for_student_data(reason: str) -> MPDExtraction:
    """The only shape a student-data hit may return: nothing but the flag."""
    return MPDExtraction(student_data_detected=True, evidence={"_blocked": reason})
