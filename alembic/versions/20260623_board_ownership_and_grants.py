"""board ownership + grants (attr_kind lazy-bind)

Карта: cards/board/feature/2026-06-23-board-ownership-and-grants.md.
D4 переписан 2026-06-27 после #137 (auth handle + multi-channel attrs):
шарим не только по email, а по любому из {email, telegram, handle}.

`boards.owner_uuid` — владелец доски (NULL = orphan, существующие
доски Юрий распределит вручную через curator UI; см. D1).

`board_grants` — расшаривание по attribute-каналу с lazy bind UUID.
- PK (board_id, subject_attr_kind, subject_attr_value).
- subject_attr_kind ∈ {email, telegram, handle}.
- subject_attr_value: lowercased для email/handle; str(tg_id) для telegram.
- subject_uuid резолвится при первом hit'е от юзера, у которого один
  из X-User-{Email,Telegram,Handle} совпал с парой (kind, value).
- level 200=read / 300=write (симметрия с auth, D3).
- index по subject_uuid для visible_boards_query.

Revision ID: board_ownership_and_grants
Revises: extref_idx_del_aware
Create Date: 2026-06-23 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "board_ownership_and_grants"
down_revision: Union[str, None] = "extref_idx_del_aware"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "boards",
        sa.Column("owner_uuid", sa.Uuid(), nullable=True),
    )
    op.create_index("ix_boards_owner_uuid", "boards", ["owner_uuid"])

    op.create_table(
        "board_grants",
        sa.Column(
            "board_id",
            sa.Uuid(),
            sa.ForeignKey("boards.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("subject_attr_kind", sa.String(16), nullable=False),
        sa.Column("subject_attr_value", sa.Text(), nullable=False),
        sa.Column("subject_uuid", sa.Uuid(), nullable=True),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("granted_by_uuid", sa.Uuid(), nullable=False),
        sa.Column("granted_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "level IN (200, 300)", name="ck_board_grants_level"
        ),
        sa.CheckConstraint(
            "subject_attr_kind IN ('email', 'telegram', 'handle')",
            name="ck_board_grants_attr_kind",
        ),
        sa.PrimaryKeyConstraint(
            "board_id",
            "subject_attr_kind",
            "subject_attr_value",
            name="pk_board_grants",
        ),
    )
    op.create_index(
        "ix_board_grants_subject_uuid",
        "board_grants",
        ["subject_uuid"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_board_grants_subject_uuid", table_name="board_grants"
    )
    op.drop_table("board_grants")
    op.drop_index("ix_boards_owner_uuid", table_name="boards")
    op.drop_column("boards", "owner_uuid")
