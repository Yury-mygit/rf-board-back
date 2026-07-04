"""Schemas for share / revoke / patch endpoints (BRD-3).

Capability-model (BRD-3 D1-D2):
- Grant хранит три булевых `can_read/can_write/can_share`.
- Инварианты: write→read, share→read, at-least-one-true.

BRD-1 D4 attribute-канал остаётся:
- Шарим по `email | telegram | handle`.
- subject_uuid резолвится lazy-bind при первом hit'е от matching юзера.
"""
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.schemas.common import CamelModel


AttrKind = Literal["email", "telegram", "handle"]


class _CapabilityBase(CamelModel):
    """Общий валидатор инвариантов capability-model."""

    can_read: bool
    can_write: bool
    can_share: bool

    @model_validator(mode="after")
    def _invariants(self):
        if self.can_write and not self.can_read:
            raise ValueError("can_write requires can_read")
        if self.can_share and not self.can_read:
            raise ValueError("can_share requires can_read")
        if not (self.can_read or self.can_write or self.can_share):
            raise ValueError("at least one capability must be true")
        return self


class GrantCreate(_CapabilityBase):
    """Share-запрос. attr_kind = email | telegram | handle; attr_value
    санитизируется на сервере (lowercased для email/handle, str для tg).

    Capability-set валидируется через _CapabilityBase invariants.
    can_share partial delegation (BRD-3 D4) — enforce на уровне endpoint
    (не схемы): non-owner/non-curator с can_share=true может слать только
    `{r=t, w=f, s=f}`; endpoint отдаст 403 иначе.
    """

    attr_kind: AttrKind
    attr_value: str = Field(..., min_length=1, max_length=255)

    @field_validator("attr_value")
    @classmethod
    def _trim(cls, v: str) -> str:
        return v.strip()


class GrantUpdate(_CapabilityBase):
    """PATCH capability-set у существующего grant'а. Owner/curator only
    (см. endpoint). Инварианты те же."""

    pass


class GrantResponse(CamelModel):
    subject_attr_kind: AttrKind
    subject_attr_value: str
    subject_uuid: UUID | None = None
    can_read: bool
    can_write: bool
    can_share: bool
    granted_by_uuid: UUID
    granted_at: int


class TransferRequest(CamelModel):
    """Передача владельца доски (BRD-1 Stage 5).

    Target должен уже быть в grants с резолвленным subject_uuid (т.е.
    хотя бы раз заходил в board под нужным attribute). Новый owner
    лишается своих grant-строк (он теперь owner, не нужны). Старый
    owner добавляется в grants как `{r=t, w=t, s=f}` (Q2 из BRD-1).
    """
    target_uuid: UUID


class TransferResponse(CamelModel):
    board_id: UUID
    new_owner_uuid: UUID
    old_owner_uuid: UUID
