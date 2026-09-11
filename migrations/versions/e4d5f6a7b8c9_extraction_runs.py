"""Add extraction_runs - the marker that makes `extract --reextract` incremental

`reextract` re-read EVERY stored document on every invocation, so its cost grew
with the corpus while the new data per run shrank. At national coverage that is
~50k documents re-parsed - and re-OCR'd, tens of seconds each on the scanned fee
PDFs - to pick up a few hundred newly fetched pages. The stage would eventually
cost more than the fetching it follows.

One row per (document, extractor pipeline version) records that the pipeline has
already looked at those bytes. A row is written whatever the OUTCOME - a
document that yielded nothing, an unreadable scan, bytes the antivirus
quarantined. Those are the expensive cases and precisely the ones that must not
be retried every run; keying off `observations` alone would miss them, because a
document that yields nothing writes no observation.

The `extractor` column holds the whole pipeline's version, derived by joining the
tier version strings (see PIPELINE_VERSION in extract/mpd/run.py). Bumping any
one extractor's `_v1` to `_v2` therefore invalidates every marker on its own,
with nothing to remember to update by hand. That is the ADR-009 promise -
improve a parser, apply it to stored bytes without re-crawling - paid for only
when a parser actually changes.

ON DELETE CASCADE tracks the one deletion the system performs (a document found
to contain student data, COMPLIANCE.md). Hard rule 4 is untouched: this table
holds no observations, no fetches and no documents, only a note of what has been
read.

Revision ID: e4d5f6a7b8c9
Revises: d3c4e5f6a7b8
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4d5f6a7b8c9"
down_revision: str | None = "d3c4e5f6a7b8"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "extraction_runs",
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("extractor", sa.Text(), nullable=False),
        sa.Column(
            "ran_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["content_hash"], ["raw_documents.content_hash"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("content_hash", "extractor"),
    )


def downgrade() -> None:
    op.drop_table("extraction_runs")
