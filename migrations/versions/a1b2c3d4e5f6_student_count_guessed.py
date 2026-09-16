"""student_count_guessed - population-statistic estimate, kept out of the verified column

Revision ID: a1b2c3d4e5f6
Revises: f5e6a7b8c9d0
"""

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "f5e6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Its own column, never `total_enrollment`. Unlike enrollment_is_estimated
    # (derived from THIS school's own teacher count), this value has no
    # per-school evidence at all - it is sampled from the DB-wide distribution.
    # Hard rule 6 says a guess must never pass as a published fact, so it stays
    # out of `total_enrollment`, `resolve`, `score` and `pcm_12_count` entirely.
    op.add_column(
        "institutions", sa.Column("student_count_guessed", sa.Integer(), nullable=True)
    )
    op.add_column(
        "institutions",
        sa.Column("student_count_guessed_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_column("institutions", "student_count_guessed_at")
    op.drop_column("institutions", "student_count_guessed")
