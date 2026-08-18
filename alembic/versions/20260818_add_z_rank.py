"""BRD-30: add z_rank VARCHAR(64) на board_elements + full backfill.

D6-revised: alembic full backfill в one-shot. Per-board fetch → rebalance
через `app.core.lexorank.rebalance()` → UPDATE. После — NOT NULL constraint.

Revision ID: add_z_rank
Revises: composite_rename
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.core.lexorank import rebalance

revision: str = "add_z_rank"
down_revision: str | None = "composite_rename"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add nullable column.
    op.add_column(
        "board_elements",
        sa.Column("z_rank", sa.String(length=64), nullable=True),
    )

    # 2. Backfill per-board. Deleted rows тоже получают z_rank —
    #    иначе restore-элемента даст NULL и sort сломается.
    conn = op.get_bind()
    boards = conn.execute(
        sa.text("SELECT DISTINCT board_id FROM board_elements")
    ).scalars().all()

    for board_id in boards:
        rows = conn.execute(
            sa.text(
                "SELECT id FROM board_elements "
                "WHERE board_id = :bid "
                "ORDER BY z_index ASC, id ASC"
            ).bindparams(bid=board_id)
        ).scalars().all()
        if not rows:
            continue
        ranks = rebalance(list(rows))  # list(rows) — не важно значение, важна длина
        # rebalance берёт list[str], но нам нужны только N ranks — фиксим:
        ranks = rebalance([""] * len(rows))
        for element_id, rank in zip(rows, ranks):
            conn.execute(
                sa.text(
                    "UPDATE board_elements SET z_rank = :r WHERE id = :i"
                ).bindparams(r=rank, i=element_id)
            )

    # 3. NOT NULL + index (для ORDER BY z_rank ASC).
    op.alter_column("board_elements", "z_rank", nullable=False)
    op.create_index(
        "ix_board_elements_board_z_rank",
        "board_elements",
        ["board_id", "z_rank"],
    )


def downgrade() -> None:
    op.drop_index("ix_board_elements_board_z_rank", table_name="board_elements")
    op.drop_column("board_elements", "z_rank")
