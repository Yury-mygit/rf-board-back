from typing import Literal
from uuid import UUID

from app.schemas.common import CamelModel


class BoardCreate(CamelModel):
    id: UUID
    title: str = "Без названия"
    created_at: int
    updated_at: int


class BoardPatch(CamelModel):
    title: str | None = None
    order_index: int | None = None
    updated_at: int


class BoardResponse(CamelModel):
    id: UUID
    title: str
    order_index: int = 0
    created_at: int
    updated_at: int
    deleted_at: int | None
    # BRD-1: владелец (NULL = orphan, видим только curator'у).
    owner_uuid: UUID | None = None
    # BRD-3 D5: capability-флаги текущего юзера. Legacy `yourRole` строка
    # (owner|curator|write|read) удалена — клиент читает bool-ы напрямую.
    is_owner: bool = False
    is_curator: bool = False
    can_read: bool = False
    can_write: bool = False
    can_share: bool = False


class BoardElementCreate(CamelModel):
    id: UUID
    type: str
    parent_id: UUID | None = None
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    attrs: dict = {}
    created_at: int
    updated_at: int


class BoardElementResponse(CamelModel):
    id: UUID
    board_id: UUID
    type: str
    external_ref: UUID | None = None
    parent_id: UUID | None
    z_index: int
    x: float
    y: float
    w: float
    h: float
    attrs: dict
    created_at: int
    updated_at: int
    deleted_at: int | None


class BoardElementUpsertByRef(CamelModel):
    """Upsert элемента по `external_ref`.

    Если элемент с такой парой (board_id, external_ref) уже есть —
    обновляем его поля (id игнорируется, чтобы не ломать FK). Если
    нет — создаём с переданным `id`.

    `attrs` — JSONB free-form. Notable type-specific keys (см. также
    frontend `board.js` и `frames.py`):
    - text: `text, fontSize, color, bold, italic, underline,
      wrap`. `wrap=true` → wrap-mode: явная `w/h`, word-wrap по
      ширине (HTML + SVG/PNG). Default = single-line label.
    - rect: `fill, stroke, strokeWidth, rx, fillOpacity, strokeOpacity`.
    - note: `text, fontSize, color, fill, stroke, strokeWidth, rx,
      autoFit`.
    """
    external_ref: UUID
    id: UUID
    type: str
    parent_id: UUID | None = None
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    attrs: dict = {}
    created_at: int
    updated_at: int


class BoardElementPatchFields(CamelModel):
    """BRD-24: patch payload без updated_at — endpoint генерирует единый ts."""
    x: float | None = None
    y: float | None = None
    w: float | None = None
    h: float | None = None
    z_index: int | None = None
    attrs: dict | None = None
    parent_id: UUID | None = None


class BoardElementBatchItem(CamelModel):
    id: UUID
    op: Literal["patch", "delete"]
    patch: BoardElementPatchFields | None = None


class BoardElementsBatchRequest(CamelModel):
    items: list[BoardElementBatchItem]


class BoardElementsBatchResponse(CamelModel):
    applied: list[UUID]
    skipped: list[dict]  # [{"id": UUID, "reason": str}]


class BoardElementZOrderRequest(CamelModel):
    """BRD-6/30: z-order op с опциональным multi-select (element_ids).

    URL содержит `{element_id}` — primary/anchor. Если `element_ids`
    задан, он полностью заменяет target set (URL id используется только
    для routing + permission-scope). Иначе target = [{url element_id}].

    BRD-30 op="between":
    - `before_id` — target(s) кладутся ПОД этим (rank < before_id.z_rank).
    - `after_id` — target(s) кладутся НАД этим (rank > after_id.z_rank).
    - Хотя бы один из двух обязателен (иначе 400).

    BRD-35: `cascade_frame` field удалён. Backend больше не выполняет
    hidden cascade для frame — caller обязан включить children через
    `element_ids` explicit'но.
    """
    op: Literal["front", "back", "forward", "backward", "between"]
    element_ids: list[UUID] | None = None
    before_id: UUID | None = None
    after_id: UUID | None = None


class BoardElementZOrderItem(CamelModel):
    id: UUID
    z_index: int
    z_rank: str
    # BRD-36: cross_parent warning убран — заменено auto-reparent (backend
    # меняет parent_id и включает в composite delta для undo).


class BoardElementZOrderResponse(CamelModel):
    items: list[BoardElementZOrderItem]


class BoardFull(CamelModel):
    id: UUID
    title: str
    order_index: int = 0
    created_at: int
    updated_at: int
    deleted_at: int | None
    owner_uuid: UUID | None = None
    is_owner: bool = False
    is_curator: bool = False
    can_read: bool = False
    can_write: bool = False
    can_share: bool = False
    elements: list[BoardElementResponse]
