import uuid

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Board(Base):
    __tablename__ = "boards"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    deleted_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Порядок в списке досок (drag-drop reorder). См. карту
    # cards/board/feature/2026-05-30-board-ui-drawer-and-palette.md.
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Владелец доски. NULL = orphan (доски, созданные до миграции
    # board_ownership_and_grants). См. карту 2026-06-23-board-ownership-and-grants.md.
    owner_uuid: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), nullable=True, index=True
    )

    elements: Mapped[list["BoardElement"]] = relationship(
        "BoardElement", back_populates="board"
    )
    grants: Mapped[list["BoardGrant"]] = relationship(
        "BoardGrant", back_populates="board", cascade="all, delete-orphan"
    )


class BoardElement(Base):
    __tablename__ = "board_elements"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True)
    board_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("boards.id"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    # Стабильный internal-to-external identifier для auto_designer-flow.
    # Уникален в пределах board (partial unique index, см. миграцию
    # 20260530_external_ref_on_board_elements). Скрипты делают upsert
    # по нему — внутренний `id` сохраняется между запусками.
    external_ref: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), nullable=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("board_elements.id"), nullable=True, index=True
    )
    z_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    x: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    y: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    w: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    h: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    attrs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    deleted_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    board: Mapped["Board"] = relationship("Board", back_populates="elements")


class BoardGrant(Base):
    """Шаринг доски по attribute-каналу с lazy-bind до UUID.

    BRD-3: ordinal level 200/300 заменён на три независимых булевых
    capability `can_read/can_write/can_share`. Инварианты (CHECK):
    write→read, share→read, at-least-one.

    D4 (BRD-1): grant хранит канал-attribute (email|telegram|handle) +
    значение; `subject_uuid` резолвится при первом hit'е от юзера, у
    которого один из его X-User-* header'ов совпал с (attr_kind,
    attr_value). Никаких лукапов в auth → no existence leak.
    """
    __tablename__ = "board_grants"
    __table_args__ = (
        PrimaryKeyConstraint(
            "board_id",
            "subject_attr_kind",
            "subject_attr_value",
            name="pk_board_grants",
        ),
        CheckConstraint(
            "subject_attr_kind IN ('email', 'telegram', 'handle')",
            name="ck_board_grants_attr_kind",
        ),
        CheckConstraint(
            "NOT can_write OR can_read",
            name="ck_board_grants_write_implies_read",
        ),
        CheckConstraint(
            "NOT can_share OR can_read",
            name="ck_board_grants_share_implies_read",
        ),
        CheckConstraint(
            "can_read OR can_write OR can_share",
            name="ck_board_grants_at_least_one",
        ),
    )

    board_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("boards.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Канал шаринга. Совпадает с одним из X-User-{Email,Telegram,Handle}.
    subject_attr_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # Значение attribute. Lowercased для email и handle; str(tg_id) для
    # telegram. На стороне POST endpoint санитизируется до записи.
    subject_attr_value: Mapped[str] = mapped_column(Text, nullable=False)
    # Резолвится при первом hit'е от юзера с этим (kind, value) (lazy bind).
    subject_uuid: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), nullable=True, index=True
    )
    # Capability-model (BRD-3). Инварианты в CheckConstraint выше.
    can_read: Mapped[bool] = mapped_column(Boolean, nullable=False)
    can_write: Mapped[bool] = mapped_column(Boolean, nullable=False)
    can_share: Mapped[bool] = mapped_column(Boolean, nullable=False)
    granted_by_uuid: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    granted_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    board: Mapped["Board"] = relationship("Board", back_populates="grants")
