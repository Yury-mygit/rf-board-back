"""Share/revoke endpoints для доски.

Карта: cards/board/feature/2026-06-23-board-ownership-and-grants.md
(Stage 4 + D4-rework 2026-06-27 / R2). Доступ к управлению grant'ами
— только owner или curator (D5, D6).

D4 (после #137): шарим по attribute-каналу `email | telegram | handle`.
Lazy-bind UUID на стороне require_board при первом hit'е.
"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_ctx import AuthCtx, current_user
from app.core.database import get_db
from app.core.deps import verify_token
from app.core.exceptions import APIError
from app.core.utils import now_ms
from app.models.models import Board, BoardGrant
from app.schemas.grant import (
    AttrKind,
    GrantCreate,
    GrantResponse,
    TransferRequest,
    TransferResponse,
)


router = APIRouter(prefix="/boards", tags=["grants"])

_ALLOWED_LEVELS = {200, 300}


async def _owner_or_curator(
    db: AsyncSession, ctx: AuthCtx, board_id: UUID
) -> Board:
    """Gate для grant-операций: owner ИЛИ curator (никаких grant-level
    делегаций — управлять grant'ами могут только эти двое)."""
    board = await db.get(Board, board_id)
    if not board or board.deleted_at is not None:
        raise APIError(
            404, "board_not_found", f"Board with id '{board_id}' does not exist"
        )
    if not ctx.is_curator and board.owner_uuid != ctx.user_uuid:
        raise APIError(
            403, "forbidden", "Only board owner or curator can manage grants"
        )
    return board


def _sanitize_attr(kind: AttrKind, value: str) -> str:
    """Канонизируем attr_value: email/handle → lowercased; telegram → цифры.

    Telegram-id принимаем как str(int) — отвергаем нечисловые. Email
    проверяем минимально (наличие `@`); полную RFC-валидацию делает
    Pydantic при необходимости.
    """
    v = value.strip()
    if kind == "email":
        if "@" not in v or len(v) < 3:
            raise APIError(400, "invalid_attr", "email must contain '@'")
        return v.lower()
    if kind == "telegram":
        if not v.isdigit():
            raise APIError(
                400, "invalid_attr", "telegram value must be numeric tg_id"
            )
        return v
    if kind == "handle":
        # handle в auth: [a-z0-9_-]{1,32}; здесь только lowercase + length.
        if not v or len(v) > 32:
            raise APIError(
                400, "invalid_attr", "handle length must be 1..32"
            )
        return v.lower()
    raise APIError(400, "invalid_attr_kind", f"unknown attr_kind: {kind!r}")


@router.get("/{board_id}/grants", response_model=list[GrantResponse])
async def list_grants(
    board_id: UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
    _: None = Depends(verify_token),
) -> list[BoardGrant]:
    await _owner_or_curator(db, ctx, board_id)
    rows = await db.execute(
        select(BoardGrant)
        .where(BoardGrant.board_id == board_id)
        .order_by(BoardGrant.granted_at.asc())
    )
    return list(rows.scalars().all())


@router.post(
    "/{board_id}/grants",
    response_model=GrantResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upsert_grant(
    board_id: UUID,
    body: GrantCreate,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
    _: None = Depends(verify_token),
) -> BoardGrant:
    await _owner_or_curator(db, ctx, board_id)
    if body.level not in _ALLOWED_LEVELS:
        raise APIError(
            400, "invalid_level", "level must be 200 (read) or 300 (write)"
        )
    value = _sanitize_attr(body.attr_kind, body.attr_value)

    # Self-grant check: запрещаем по любому из своих attribute. Owner и
    # так видит доску — нет смысла; и без этого UI может сбить с толку.
    self_values = {
        "email": ctx.user_email or None,
        "telegram": ctx.user_telegram or None,
        "handle": ctx.user_handle or None,
    }
    if self_values.get(body.attr_kind) == value:
        raise APIError(400, "self_grant", "Cannot grant access to yourself")

    ts = now_ms()
    stmt = (
        pg_insert(BoardGrant)
        .values(
            board_id=board_id,
            subject_attr_kind=body.attr_kind,
            subject_attr_value=value,
            subject_uuid=None,
            level=body.level,
            granted_by_uuid=ctx.user_uuid,
            granted_at=ts,
        )
        .on_conflict_do_update(
            index_elements=[
                "board_id", "subject_attr_kind", "subject_attr_value"
            ],
            set_={
                "level": body.level,
                "granted_by_uuid": ctx.user_uuid,
                "granted_at": ts,
            },
        )
    )
    await db.execute(stmt)
    await db.commit()
    row = (
        await db.execute(
            select(BoardGrant).where(
                BoardGrant.board_id == board_id,
                BoardGrant.subject_attr_kind == body.attr_kind,
                BoardGrant.subject_attr_value == value,
            )
        )
    ).scalar_one()
    return row


@router.delete(
    "/{board_id}/grants/{attr_kind}/{attr_value}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_grant(
    board_id: UUID,
    attr_kind: AttrKind,
    attr_value: str,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
    _: None = Depends(verify_token),
) -> None:
    await _owner_or_curator(db, ctx, board_id)
    value = _sanitize_attr(attr_kind, attr_value)
    grant = (
        await db.execute(
            select(BoardGrant).where(
                BoardGrant.board_id == board_id,
                BoardGrant.subject_attr_kind == attr_kind,
                BoardGrant.subject_attr_value == value,
            )
        )
    ).scalar_one_or_none()
    if grant is not None:
        await db.delete(grant)
        await db.commit()


@router.post(
    "/{board_id}/transfer",
    response_model=TransferResponse,
)
async def transfer_ownership(
    board_id: UUID,
    body: TransferRequest,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
    _: None = Depends(verify_token),
) -> TransferResponse:
    """Передать владельца доски. Карта #130 Stage 5.

    Owner-only. Curator передаёт через `/admin/boards/{id}/assign-owner`
    (Stage 6) — отдельный flow для orphan-досок.

    Target должен уже быть в grants с резолвленным subject_uuid
    (D6: target_must_be_member_first). Транзакция:
    1. boards.owner_uuid → target_uuid;
    2. удаляем все grant-строки target (он теперь owner);
    3. вставляем grant для старого owner: subject_uuid prefilled,
       attr_kind/value берём из ctx (email > handle), level=300 (Q2).
    """
    board = await db.get(Board, board_id)
    if not board or board.deleted_at is not None:
        raise APIError(
            404, "board_not_found", f"Board with id '{board_id}' does not exist"
        )
    if board.owner_uuid != ctx.user_uuid:
        raise APIError(
            403,
            "forbidden",
            "Only board owner can transfer ownership (curator uses /admin/assign-owner)",
        )
    if body.target_uuid == ctx.user_uuid:
        raise APIError(
            400, "self_transfer", "Cannot transfer ownership to yourself"
        )

    # Target должен быть в grants с резолвленным subject_uuid.
    target_grants = (
        await db.execute(
            select(BoardGrant).where(
                BoardGrant.board_id == board_id,
                BoardGrant.subject_uuid == body.target_uuid,
            )
        )
    ).scalars().all()
    if not target_grants:
        raise APIError(
            400,
            "target_must_be_member_first",
            "Target user must already have a resolved grant on this board "
            "(share + first login required before transfer)",
        )

    old_owner_uuid = ctx.user_uuid

    # Выбор attribute для старого owner — email > handle (handle всегда
    # non-empty после #137; email может быть пустой для TG-only юзеров).
    if ctx.user_email:
        demoted_attr_kind = "email"
        demoted_attr_value = ctx.user_email
    elif ctx.user_handle:
        demoted_attr_kind = "handle"
        demoted_attr_value = ctx.user_handle
    else:
        # Defensive: handle всегда должен быть, но если как-то пусто —
        # 400 чтобы owner понял что у него аккаунт без identifying attrs.
        raise APIError(
            400,
            "no_demote_attr",
            "Cannot demote old owner: no email or handle in identity",
        )

    ts = now_ms()

    # 1. Сменить owner.
    board.owner_uuid = body.target_uuid

    # 2. Удалить grants target'а (он теперь owner).
    for g in target_grants:
        await db.delete(g)

    # 3. Вставить grant для старого owner. ON CONFLICT DO UPDATE на случай
    # если у него уже был grant (теоретически невозможно, но defensive).
    stmt = (
        pg_insert(BoardGrant)
        .values(
            board_id=board_id,
            subject_attr_kind=demoted_attr_kind,
            subject_attr_value=demoted_attr_value,
            subject_uuid=old_owner_uuid,
            level=300,
            granted_by_uuid=old_owner_uuid,
            granted_at=ts,
        )
        .on_conflict_do_update(
            index_elements=[
                "board_id", "subject_attr_kind", "subject_attr_value"
            ],
            set_={
                "subject_uuid": old_owner_uuid,
                "level": 300,
                "granted_by_uuid": old_owner_uuid,
                "granted_at": ts,
            },
        )
    )
    await db.execute(stmt)
    await db.commit()

    return TransferResponse(
        board_id=board_id,
        new_owner_uuid=body.target_uuid,
        old_owner_uuid=old_owner_uuid,
    )
