"""Add total_teachers and enrollment_is_estimated to institutions

Two small columns for the handover build. The sales team asked for as much
context per school as possible, and the Appendix-IX proforma publishes
"TOTAL NO. OF TEACHERS" far more often than it publishes student counts.

`enrollment_is_estimated` exists so an approximated student count is never
displayed as though the school published it. The estimate is teachers x a
configured ratio, with the arithmetic kept in the observation's evidence_span;
this flag is what makes the UI and export render it as "~N (estimated)".

Revision ID: c2b3d4e5f6a7
Revises: b1a2c3d4e5f6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2b3d4e5f6a7"
down_revision: str | None = "b1a2c3d4e5f6"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "institutions", sa.Column("total_teachers", sa.Integer(), nullable=True)
    )
    op.add_column(
        "institutions",
        sa.Column(
            "enrollment_is_estimated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("institutions", "enrollment_is_estimated")
    op.drop_column("institutions", "total_teachers")
