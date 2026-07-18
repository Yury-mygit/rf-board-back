"""BRD-24 foundation: rename existing composite kind → mixed

Existing `kind="composite"` в БД (BRD-18 classify_patch) означало «single-target
mixed-field snapshot» (одновременное изменение x/y+attrs). BRD-24 переопределяет
`composite` под multi-target с per-item heterogeneous delta. Existing rows
переименовываются в `kind="mixed"` чтобы не пересекаться с новым семантикой.

Revision ID: composite_rename
Revises: undo_foundation
"""
from alembic import op


revision: str = "composite_rename"
down_revision: str | None = "undo_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE board_actions SET kind = 'mixed' WHERE kind = 'composite'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE board_actions SET kind = 'composite' WHERE kind = 'mixed'"
    )
