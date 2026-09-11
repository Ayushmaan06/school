"""Load scoring.yaml. The ONLY place scoring numbers enter the program.

Hard rule 8: never hardcode a weight, band or geography value in Python. Every
number below comes out of `scoring.yaml`, so changing a weight is a config edit
plus a model_version bump rather than a code change.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

SCORING_PATH = Path(__file__).with_name("scoring.yaml")


@lru_cache(maxsize=1)
def scoring() -> dict[str, Any]:
    return yaml.safe_load(SCORING_PATH.read_text("utf-8"))


def model_version() -> str:
    return scoring()["model_version"]


def component_max() -> dict[str, int]:
    return scoring()["components"]


def min_possible_points() -> int:
    return scoring()["min_possible_points"]


def band_points(
    bands: list[dict], value: float | None, key: str = "upto"
) -> tuple[int, str]:
    """Pick a band. `upto` bands are exclusive upper bounds; `from` are inclusive
    lower bounds. Returns (points, reason)."""
    if value is None:
        return 0, "unknown"
    if key == "upto":
        for band in bands:
            limit = band[key]
            if limit is None or value < limit:
                return band["points"], band.get("reason", "")
        return bands[-1]["points"], bands[-1].get("reason", "")
    for band in bands:
        if value >= band["from"]:
            return band["points"], band.get("reason", "")
    return bands[-1]["points"], bands[-1].get("reason", "")


def affordability_bands() -> list[dict]:
    return scoring()["affordability_bands"]


def pcm_bands() -> list[dict]:
    return scoring()["pcm_12_bands"]


def pcm_estimation() -> dict:
    return scoring()["pcm_12_estimation"]


def curriculum_points() -> dict[str, int]:
    return scoring()["curriculum_points"]


def geography(campaign: str = "default") -> dict:
    table = scoring()["geography"]
    if campaign not in table:
        raise KeyError(
            f"campaign {campaign!r} has no geography table in scoring.yaml. "
            "Campaigns are config, so add one there rather than defaulting - "
            "silently falling back would score a campaign as if it were another."
        )
    return table[campaign]


def accessibility() -> dict:
    return scoring()["accessibility"]


def group_bands() -> list[dict]:
    return scoring()["group_leverage_bands"]


def franchise_cap() -> int:
    return scoring()["franchise_network_cap"]


def confidence_config() -> dict:
    return scoring()["confidence"]


def hard_gate() -> dict:
    return scoring()["hard_gate"]
