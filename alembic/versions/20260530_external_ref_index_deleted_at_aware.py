"""external_ref unique index → ignore soft-deleted rows

Старый partial unique `WHERE external_ref IS NOT NULL` блокировал
повторный INSERT с тем же external_ref если предыдущий row был
soft-deleted (`deleted_at IS NOT NULL`). Это ломает auto_designer
после ручной очистки доски через UPDATE deleted_at.

Новый partial: `WHERE external_ref IS NOT NULL AND deleted_at IS NULL`.

Revision ID: extref_idx_del_aware
Revises: boards_order_index
Create Date: 2026-05-30 21:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "extref_idx_del_aware"
down_revision: Union[str, None] = "boards_order_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(
        "ix_board_elements_external_ref_unique",
        table_name="board_elements",
    )
    op.create_index(
        "ix_board_elements_external_ref_unique",
        "board_elements",
        ["board_id", "external_ref"],
        unique=True,
        postgresql_where=sa.text(
            "external_ref IS NOT NULL AND deleted_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_board_elements_external_ref_unique",
        table_name="board_elements",
    )
    op.create_index(
        "ix_board_elements_external_ref_unique",
        "board_elements",
        ["board_id", "external_ref"],
        unique=True,
        postgresql_where=sa.text("external_ref IS NOT NULL"),
    )
