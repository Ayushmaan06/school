"""Add registry_snapshots.scope

The drift guard must compare like with like. [VERIFIED M5] a national snapshot
(33,146 rows) followed by a single-state run (2,252 rows) tripped hard rule 10
at "7%" and aborted the import - correct arithmetic against the wrong baseline.
`scope` records which states a snapshot covers so only comparable runs are
compared. It also matters for M4-3: diffing a national snapshot against a
state one would emit thousands of false disaffiliation signals, which is the
exact failure the guard exists to prevent.

Revision ID: b1a2c3d4e5f6
Revises: a62131789911
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1a2c3d4e5f6"
down_revision: str | None = "a62131789911"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("registry_snapshots", sa.Column("scope", sa.Text(), nullable=True))
    op.create_index(
        "ix_registry_snapshots_source_scope",
        "registry_snapshots",
        ["source_id", "scope", sa.text("taken_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_registry_snapshots_source_scope", table_name="registry_snapshots")
    op.drop_column("registry_snapshots", "scope")
