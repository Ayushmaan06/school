"""Add institutions.last_enriched_at

Makes the enrichment pass RESUMABLE. Without it, re-running after an
interruption starts from the beginning and re-fetches schools already done -
which on a multi-hour national run means losing an evening's work and being
rude to the same websites twice.

It also implements the docs/SOURCES.md S2 refresh cadence: a school enriched
within `refresh_days` is skipped, so a re-run picks up where it stopped rather
than redoing settled work.

Survives `resolve`, because institutions are upserted on their stable-ID column
and this column is not part of the upsert payload.

Revision ID: d3c4e5f6a7b8
Revises: c2b3d4e5f6a7
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3c4e5f6a7b8"
down_revision: str | None = "c2b3d4e5f6a7"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "institutions",
        sa.Column("last_enriched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_institutions_last_enriched",
        "institutions",
        ["last_enriched_at"],
        postgresql_where=sa.text("last_enriched_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_institutions_last_enriched", table_name="institutions")
    op.drop_column("institutions", "last_enriched_at")
