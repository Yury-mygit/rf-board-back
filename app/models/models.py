import uuid

from sqlalchemy import (
    JSON,
    BigInteger,
    Float,
    ForeignKey,
    Integer,
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

    elements: Mapped[list["BoardElement"]] = relationship(
        "BoardElement", back_populates="board"
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
