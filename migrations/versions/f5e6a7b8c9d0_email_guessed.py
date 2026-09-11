"""email_guessed - pattern-derived address, kept out of the verified column

Revision ID: f5e6a7b8c9d0
Revises: e4d5f6a7b8c9
"""

import sqlalchemy as sa
from alembic import op

revision = "f5e6a7b8c9d0"
down_revision = "e4d5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Its own column, never `email`. Hard rule 6 is about a guess PASSING AS a
    # published fact; a column whose name says "guessed" cannot. Keeping it
    # separate is also what lets `resolve`, `score` and the contactable counts
    # stay honest - none of them read this.
    op.add_column(
        "institutions", sa.Column("email_guessed", sa.Text(), nullable=True)
    )
    op.add_column(
        "institutions", sa.Column("email_guessed_at", sa.DateTime(timezone=True))
    )


def downgrade() -> None:
    op.drop_column("institutions", "email_guessed_at")
    op.drop_column("institutions", "email_guessed")
