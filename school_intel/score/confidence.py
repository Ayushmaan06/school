"""Confidence: how much should a BD person trust the Fit number?

A separate 0-100. **Never added to Fit and never used to sort by default**
(ADR-010). It is displayed alongside Fit, available as a filter, and doubles as
a work queue: "high Fit, low Confidence" is exactly the enrichment backlog.

Three parts: how complete the decision-critical fields are, how authoritative
the winning sources were, and how old the oldest of them is.
"""

from dataclasses import dataclass
from datetime import datetime

from school_intel.score import config


@dataclass
class ConfidenceResult:
    confidence: float
    completeness: float
    authority: float
    freshness: float
    present_fields: list[str]
    missing_fields: list[str]

    def as_dict(self) -> dict:
        return {
            "confidence": self.confidence,
            "completeness": round(self.completeness, 2),
            "authority": round(self.authority, 2),
            "freshness": round(self.freshness, 2),
            "present": self.present_fields,
            "missing": self.missing_fields,
        }


def compute(
    present: dict[str, bool],
    authority_tiers: list[int],
    oldest_observed_at: datetime | None,
    now: datetime,
) -> ConfidenceResult:
    cfg = config.confidence_config()
    critical = cfg["decision_critical_fields"]

    have = [f for f in critical if present.get(f)]
    missing = [f for f in critical if not present.get(f)]
    completeness = cfg["field_completeness_max"] * (len(have) / len(critical))

    if authority_tiers:
        points = cfg["authority_tier_points"]
        mean = sum(points.get(t, 0) for t in authority_tiers) / len(authority_tiers)
        authority = min(mean, cfg["source_authority_max"])
    else:
        authority = 0.0

    freshness = 0.0
    if oldest_observed_at is not None:
        months = (now - oldest_observed_at).days / 30.44
        for band in cfg["freshness_months"]:
            limit = band["upto"]
            if limit is None or months <= limit:
                freshness = band["points"]
                break

    total = round(min(completeness + authority + freshness, 100), 1)
    return ConfidenceResult(total, completeness, authority, freshness, have, missing)
