"""Schemas for share / revoke endpoints.

Карта #130 D4 (переписана 2026-06-27 после #137):
- Шарим по attribute-каналу `email | telegram | handle`.
- subject_uuid резолвится lazy-bind при первом hit'е от matching юзера.
- Никаких лукапов в auth → no existence leak.
"""
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.common import CamelModel


AttrKind = Literal["email", "telegram", "handle"]


class GrantCreate(CamelModel):
    """Share-запрос. attr_kind = email | telegram | handle; attr_value
    санитизируется на сервере (lowercased для email/handle, str для tg)."""

    attr_kind: AttrKind
    attr_value: str = Field(..., min_length=1, max_length=255)
    level: int = Field(..., description="200=read, 300=write")

    @field_validator("attr_value")
    @classmethod
    def _trim(cls, v: str) -> str:
        return v.strip()


class GrantResponse(CamelModel):
    subject_attr_kind: AttrKind
    subject_attr_value: str
    subject_uuid: UUID | None = None
    level: int
    granted_by_uuid: UUID
    granted_at: int


class TransferRequest(CamelModel):
    """Передача владельца доски (Stage 5).

    Target должен уже быть в grants с резолвленным subject_uuid (т.е.
    хотя бы раз заходил в board под нужным attribute). Новый owner
    лишается своих grant-строк (он теперь owner, не нужны). Старый
    owner добавляется в grants как level=300 (Q2).
    """
    target_uuid: UUID


class TransferResponse(CamelModel):
    board_id: UUID
    new_owner_uuid: UUID
    old_owner_uuid: UUID
