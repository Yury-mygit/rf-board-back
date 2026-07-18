"""undo/redo foundation (BRD-18)

Модель γ2 из BRD-13:
- `board_elements.touchers` JSONB — множество user_uuid, кто прикасался.
- `board_actions` — event-sourced log действий, каждая mutation пишет row.

Undo/Redo endpoints и engine — в BRD-19.
UI — в BRD-20.

Revision ID: undo_foundation
Revises: grants_capability_bools
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "undo_foundation"
down_revision: Union[str, None] = "grants_capability_bools"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # touchers на BoardElement — существующие элементы получают '[]'.
    op.add_column(
        "board_elements",
        sa.Column(
            "touchers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_index(
        "ix_board_elements_touchers_gin",
        "board_elements",
        ["touchers"],
        postgresql_using="gin",
    )

    op.create_table(
        "board_actions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "board_id",
            sa.Uuid(),
            sa.ForeignKey("boards.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("executor_uuid", sa.Uuid(), nullable=False),
        sa.Column(
            "associated_users",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column(
            "target_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "delta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("ts_ms", sa.BigInteger(), nullable=False),
        sa.Column(
            "undone",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "pruned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_board_actions_board_ts",
        "board_actions",
        ["board_id", sa.text("ts_ms DESC")],
    )
    op.create_index(
        "ix_board_actions_associated_gin",
        "board_actions",
        ["associated_users"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_board_actions_associated_gin", table_name="board_actions")
    op.drop_index("ix_board_actions_board_ts", table_name="board_actions")
    op.drop_table("board_actions")
    op.drop_index("ix_board_elements_touchers_gin", table_name="board_elements")
    op.drop_column("board_elements", "touchers")
