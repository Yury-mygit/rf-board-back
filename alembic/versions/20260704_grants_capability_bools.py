"""board_grants: level int → capability bools (r/w/s)

BRD-3 D1-D3. Ordinal `level` (200 read / 300 write) заменяется на три
независимых булевых capability: `can_read`, `can_write`, `can_share`.

Инварианты (CHECK):
- `can_write → can_read` — писать без чтения бессмысленно.
- `can_share → can_read` — приглашать не имея доступа не имеет смысла.
- Хотя бы один true (all-false grant запрещён).

Backfill: level 200 → r=t/w=f/s=f, level 300 → r=t/w=t/s=f.
can_share=false для всех — существующие grant'ы не пере-делегируем
на приглашение автоматически (owner переназначит вручную).

Revision ID: grants_capability_bools
Revises: board_ownership_and_grants
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "grants_capability_bools"
down_revision: Union[str, None] = "board_ownership_and_grants"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Добавляем 3 bool NULL (пока не заполнили — NULL допустим).
    op.add_column(
        "board_grants",
        sa.Column("can_read", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "board_grants",
        sa.Column("can_write", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "board_grants",
        sa.Column("can_share", sa.Boolean(), nullable=True),
    )
    # 2. Backfill из level.
    op.execute(
        "UPDATE board_grants SET "
        "can_read = TRUE, "
        "can_write = (level = 300), "
        "can_share = FALSE"
    )
    # 3. NOT NULL после backfill'а.
    op.alter_column("board_grants", "can_read", nullable=False)
    op.alter_column("board_grants", "can_write", nullable=False)
    op.alter_column("board_grants", "can_share", nullable=False)
    # 4. Дропаем legacy level + его CHECK.
    op.drop_constraint("ck_board_grants_level", "board_grants", type_="check")
    op.drop_column("board_grants", "level")
    # 5. Инварианты capability-model.
    op.create_check_constraint(
        "ck_board_grants_write_implies_read",
        "board_grants",
        "NOT can_write OR can_read",
    )
    op.create_check_constraint(
        "ck_board_grants_share_implies_read",
        "board_grants",
        "NOT can_share OR can_read",
    )
    op.create_check_constraint(
        "ck_board_grants_at_least_one",
        "board_grants",
        "can_read OR can_write OR can_share",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_board_grants_at_least_one", "board_grants", type_="check"
    )
    op.drop_constraint(
        "ck_board_grants_share_implies_read", "board_grants", type_="check"
    )
    op.drop_constraint(
        "ck_board_grants_write_implies_read", "board_grants", type_="check"
    )
    op.add_column(
        "board_grants",
        sa.Column("level", sa.Integer(), nullable=True),
    )
    # Обратный backfill: r=t/w=t → 300, r=t/w=f → 200. can_share ignore
    # (в старой модели не существовало).
    op.execute(
        "UPDATE board_grants SET level = CASE WHEN can_write THEN 300 ELSE 200 END"
    )
    op.alter_column("board_grants", "level", nullable=False)
    op.create_check_constraint(
        "ck_board_grants_level",
        "board_grants",
        "level IN (200, 300)",
    )
    op.drop_column("board_grants", "can_share")
    op.drop_column("board_grants", "can_write")
    op.drop_column("board_grants", "can_read")
