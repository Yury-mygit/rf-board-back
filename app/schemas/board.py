from uuid import UUID

from app.schemas.common import CamelModel


class BoardCreate(CamelModel):
    id: UUID
    title: str = "Без названия"
    created_at: int
    updated_at: int


class BoardPatch(CamelModel):
    title: str | None = None
    updated_at: int


class BoardResponse(CamelModel):
    id: UUID
    title: str
    created_at: int
    updated_at: int
    deleted_at: int | None


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


class BoardElementPatch(CamelModel):
    x: float | None = None
    y: float | None = None
    w: float | None = None
    h: float | None = None
    z_index: int | None = None
    attrs: dict | None = None
    parent_id: UUID | None = None
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


class BoardFull(CamelModel):
    id: UUID
    title: str
    created_at: int
    updated_at: int
    deleted_at: int | None
    elements: list[BoardElementResponse]
