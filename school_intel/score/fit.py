"""Fit: six components, renormalised over the components that HAVE data.

The renormalisation is the part that matters (ADR-010). A missing value reduces
CONFIDENCE, it does not silently zero a sub-score:

    earned   = sum(points for components with data)
    possible = sum(max    for components with data)
    fit      = 100 * earned / possible

So an institution with everything except a fee is scored out of 70 and scaled
up. It competes on what we know, and its lower Confidence tells the BD user how
much to trust the number. **An institution with unknown fees is not a cheap
institution.**

Guard: below `min_possible_points` no Fit is emitted at all. Scaling 10 points
up to 100 is not a score, it is noise.
"""

from dataclasses import dataclass, field
from typing import Any

from school_intel.score import config


@dataclass
class Component:
    name: str
    raw: Any
    points: float
    max_points: int
    reason: str
    #: False when the input is unknown, which removes the component from BOTH
    #: numerator and denominator rather than scoring it zero.
    has_data: bool = True


@dataclass
class FitResult:
    fit: float | None
    components: dict[str, dict]
    flags: list[str] = field(default_factory=list)
    earned: float = 0.0
    possible: int = 0

    @property
    def insufficient_data(self) -> bool:
        return "insufficient_data" in self.flags


def affordability(fee_mid: int | None) -> Component:
    maximum = config.component_max()["affordability"]
    if fee_mid is None:
        return Component(
            "affordability", None, 0, maximum, "no fee observation", has_data=False
        )
    points, reason = config.band_points(config.affordability_bands(), fee_mid)
    return Component(
        "affordability", fee_mid, points, maximum, f"Rs {fee_mid:,}/yr: {reason}"
    )


def pcm_12_volume(
    pcm_count: int | None,
    class_12_total: int | None,
    total_enrollment: int | None,
    spans_k12: bool = True,
) -> tuple[Component, bool]:
    """Class-12 science headcount. Returns (component, was_estimated).

    NOT total enrollment: a 5,000-student K-12 school with 60 PCM students is
    worth less than an 800-student school with 200.
    """
    maximum = config.component_max()["pcm_12_volume"]
    estimation = config.pcm_estimation()
    estimated = False

    value = pcm_count
    source = "observed"
    if value is None and class_12_total is not None:
        value = round(class_12_total * estimation["from_class_12_total"])
        source = f"estimated from class-12 total {class_12_total}"
        estimated = True
    elif value is None and total_enrollment is not None and spans_k12:
        value = round(total_enrollment * estimation["from_total_enrollment"])
        source = f"estimated from total enrollment {total_enrollment}"
        estimated = True

    if value is None:
        return (
            Component(
                "pcm_12_volume",
                None,
                0,
                maximum,
                "no headcount available",
                has_data=False,
            ),
            False,
        )

    points, _ = config.band_points(config.pcm_bands(), value, key="from")
    if estimated:
        # Applied to the COMPONENT's points, not to Fit overall, so an estimated
        # 300 cannot outrank a verified 200.
        points = points * estimation["penalty_multiplier"]
    return (
        Component(
            "pcm_12_volume", value, points, maximum, f"{value} PCM class-12 ({source})"
        ),
        estimated,
    )


def curriculum_tier(boards: list[str]) -> Component:
    """Maximum over the institution's boards.

    IGCSE or MYP alone scores 0 here and usually fails the hard gate anyway,
    since those stop before class 11.
    """
    maximum = config.component_max()["curriculum_tier"]
    table = config.curriculum_points()
    if not boards:
        return Component(
            "curriculum_tier", None, 0, maximum, "no board", has_data=False
        )
    scored = {
        board: table.get(board, table.get("STATE_DEFAULT", 0))
        if not board.startswith("STATE_")
        else table.get("STATE_DEFAULT", 0)
        for board in boards
    }
    best_board = max(scored, key=lambda b: scored[b])
    points = scored[best_board]
    zero = [b for b, p in scored.items() if p == 0]
    reason = f"{best_board}"
    if zero:
        reason += f"; {', '.join(zero)} contributes 0"
    return Component("curriculum_tier", boards, points, maximum, reason)


def geography(
    district: str | None,
    metro: str | None,
    state: str | None,
    campaign: str = "default",
) -> Component:
    """Anchored on Bengaluru. A BD-efficiency weighting, not a market boundary.

    `metro_area` is the join key, not `city` - NCR spans three states and city
    alone is never a safe key (Project-Doc 6.2, 6.3).
    """
    maximum = config.component_max()["geography"]
    table = config.geography(campaign)

    if district and district.lower() in table.get("districts", {}):
        return Component(
            "geography",
            district,
            table["districts"][district.lower()],
            maximum,
            district,
        )
    if metro and metro in table.get("metros", {}):
        return Component("geography", metro, table["metros"][metro], maximum, metro)
    if state and state.lower() in table.get("states", {}):
        return Component(
            "geography", state, table["states"][state.lower()], maximum, state
        )
    if district is None and metro is None and state is None:
        return Component("geography", None, 0, maximum, "no location", has_data=False)
    return Component(
        "geography",
        metro or district or state,
        table["fallback"],
        maximum,
        "outside the corridor",
    )


def accessibility(
    has_counsellor: bool,
    principal_fresh: bool,
    has_email: bool,
    has_phone: bool,
) -> Component:
    """An institution nobody can reach is unactionable regardless of how good it
    looks. Additive, capped at the component max."""
    maximum = config.component_max()["accessibility"]
    table = config.accessibility()
    parts = []
    points = 0
    if has_counsellor:
        points += table["counsellor_or_coordinator"]
        parts.append("named counsellor")
    if principal_fresh:
        points += table["principal_verified"]
        parts.append("principal verified")
    if has_email:
        points += table["email"]
        parts.append("email")
    if has_phone:
        points += table["phone"]
        parts.append("phone")
    points = min(points, maximum)
    if not parts:
        return Component(
            "accessibility", None, 0, maximum, "no route in", has_data=False
        )
    return Component("accessibility", parts, points, maximum, ", ".join(parts))


def group_leverage(
    qualifying_campuses: int | None, group_type: str | None
) -> Component:
    """Uses qualifying_campus_count, NEVER raw campus_count.

    A `possible_franchise_network` is capped: a brand match is not a
    decision-maker, and treating a franchise as one chain sends BD to the wrong
    contact and inflates the score (Project-Doc 6.6).
    """
    maximum = config.component_max()["group_leverage"]
    if qualifying_campuses is None:
        return Component("group_leverage", None, 0, maximum, "standalone")
    points, _ = config.band_points(
        config.group_bands(), qualifying_campuses, key="from"
    )
    reason = f"{qualifying_campuses} qualifying campuses"
    if group_type == "possible_franchise_network":
        cap = config.franchise_cap()
        if points > cap:
            points = cap
            reason += f" (capped at {cap}: brand match only, not verified ownership)"
    return Component("group_leverage", qualifying_campuses, points, maximum, reason)


def combine(components: list[Component], flags: list[str] | None = None) -> FitResult:
    """Renormalise over the components that have data."""
    flags = list(flags or [])
    with_data = [c for c in components if c.has_data]
    earned = sum(c.points for c in with_data)
    possible = sum(c.max_points for c in with_data)

    blob = {
        c.name: {
            "raw": c.raw,
            "points": round(c.points, 2),
            "max": c.max_points,
            "reason": c.reason,
        }
        for c in components
    }

    if possible < config.min_possible_points():
        flags.append("insufficient_data")
        blob["_meta"] = {
            "earned": round(earned, 2),
            "possible": possible,
            "fit": None,
            "model_version": config.model_version(),
        }
        return FitResult(None, blob, flags, earned, possible)

    fit = round(100 * earned / possible, 1)
    blob["_meta"] = {
        "earned": round(earned, 2),
        "possible": possible,
        "fit": fit,
        "model_version": config.model_version(),
    }
    return FitResult(fit, blob, flags, earned, possible)
