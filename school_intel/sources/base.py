"""Importer protocol. A Protocol and a plain function are enough - no base class.

AGENTS.md: one module per source, no inheritance hierarchy.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ImportResult:
    snapshot_id: int | None
    rows_seen: int
    jobs_enqueued: int
    notes: dict[str, object]


class Importer(Protocol):
    source_id: str

    async def run(self) -> ImportResult: ...


class StructureDriftError(RuntimeError):
    """A snapshot with under 50% of the previous row count. Hard rule 10.

    Aborts the run WITHOUT writing a snapshot. Without this guard one parser
    regression emits thousands of false `disaffiliation` signals in M4-3, which
    is the worst possible output of this system: BD would be told that schools
    they are mid-conversation with have closed.
    """


class RowCountMismatchError(RuntimeError):
    """Parsed row count disagrees with the page's own stated total.

    Fail loudly (M1-1). A silent shortfall here looks exactly like "that state
    has fewer schools this month".
    """
