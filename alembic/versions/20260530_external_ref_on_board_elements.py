"""add external_ref to board_elements

Stable identifier для auto_designer-flow: один и тот же фрейм upsert-ится
по `external_ref`, его внутренний `id` остаётся стабильным между запусками.
Карта: open_cards/cards/board/feature/2026-05-30-board-external-ref-stable-id.md.

Колонка nullable — большинство элементов (rect/text внутри frame'а) не
управляются auto-design. Partial unique index `(board_id, external_ref)
WHERE external_ref IS NOT NULL` гарантирует уникальность ref в пределах
доски, не запрещая дефолтный NULL.

Revision ID: external_ref_on_elements
Revises: d497f67334b4
Create Date: 2026-05-30 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "external_ref_on_elements"
down_revision: Union[str, None] = "d497f67334b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "board_elements",
        sa.Column("external_ref", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_board_elements_external_ref_unique",
        "board_elements",
        ["board_id", "external_ref"],
        unique=True,
        postgresql_where=sa.text("external_ref IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_board_elements_external_ref_unique",
        table_name="board_elements",
    )
    op.drop_column("board_elements", "external_ref")
