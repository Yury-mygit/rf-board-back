"""boards.order_index for drag-drop reorder

Карта: open_cards/cards/board/feature/2026-05-30-board-ui-drawer-and-palette.md
(Этап 2). При reorder через PATCH /boards/{id} фронт прокидывает новый
order_index. list_boards сортирует по (order_index ASC, updated_at DESC) —
доски с одинаковым order_index сортируются по дате как раньше.

Revision ID: boards_order_index
Revises: external_ref_on_elements
Create Date: 2026-05-30 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "boards_order_index"
down_revision: Union[str, None] = "external_ref_on_elements"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "boards",
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_boards_order_index", "boards", ["order_index"])


def downgrade() -> None:
    op.drop_index("ix_boards_order_index", table_name="boards")
    op.drop_column("boards", "order_index")
