"""Grants endpoints (BRD-3 capability-model).

Endpoints:
- GET    /boards/{id}/grants                   — owner/curator only
- POST   /boards/{id}/grants                   — owner/curator: любой capability
                                                  can_share (не owner): только {r=t,w=f,s=f}
- PATCH  /boards/{id}/grants/{kind}/{value}    — owner/curator only
- DELETE /boards/{id}/grants/{kind}/{value}    — owner/curator only
- POST   /boards/{id}/transfer                 — owner-only

BRD-1 D4 attribute-канал: шарим по `email | telegram | handle`.
Lazy-bind UUID на стороне require_board.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_ctx import AuthCtx, current_user, your_capabilities_map
from app.core.database import get_db
from app.core.deps import verify_token
from app.core.exceptions import APIError
from app.core.utils import now_ms
from app.models.models import Board, BoardGrant
from app.schemas.grant import (
    AttrKind,
    GrantCreate,
    GrantResponse,
    GrantUpdate,
    TransferRequest,
    TransferResponse,
)


router = APIRouter(prefix="/boards", tags=["grants"])


async def _load_board(db: AsyncSession, board_id: UUID) -> Board:
    board = await db.get(Board, board_id)
    if not board or board.deleted_at is not None:
        raise APIError(
            404, "board_not_found", f"Board with id '{board_id}' does not exist"
        )
    return board


async def _owner_or_curator(
    db: AsyncSession, ctx: AuthCtx, board_id: UUID
) -> Board:
    """Полное управление grant'ами: owner ИЛИ curator (BRD-1 D5/D6)."""
    board = await _load_board(db, board_id)
    if not ctx.is_curator and board.owner_uuid != ctx.user_uuid:
        raise APIError(
            403, "forbidden", "Only board owner or curator can manage grants"
        )
    return board


async def _has_can_share(
    db: AsyncSession, ctx: AuthCtx, board: Board
) -> bool:
    """Есть ли у ctx capability `share` на доске (через grants)."""
    caps = await your_capabilities_map(db, ctx, [board])
    c = caps.get(board.id)
    return bool(c and c.can_share)


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
    """POST grant. Owner/curator: любой валидный capability-set.
    can_share non-owner: только {r=t, w=f, s=f}, иначе 403 (BRD-3 D4).
    """
    board = await _load_board(db, board_id)
    is_manage = ctx.is_curator or board.owner_uuid == ctx.user_uuid
    if not is_manage:
        # Проверка что у caller'а есть can_share (иначе 403).
        if not await _has_can_share(db, ctx, board):
            raise APIError(
                403,
                "forbidden",
                "Managing grants requires owner, curator, or can_share",
            )
        # Ограничение payload'а: только read-only invite.
        if not (body.can_read and not body.can_write and not body.can_share):
            raise APIError(
                403,
                "restricted_invite_read_only",
                "can_share users may only invite read-only "
                "(can_read=true, can_write=false, can_share=false)",
            )
    value = _sanitize_attr(body.attr_kind, body.attr_value)

    # Self-grant check: запрещаем по любому из своих attribute.
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
            can_read=body.can_read,
            can_write=body.can_write,
            can_share=body.can_share,
            granted_by_uuid=ctx.user_uuid,
            granted_at=ts,
        )
        .on_conflict_do_update(
            index_elements=[
                "board_id", "subject_attr_kind", "subject_attr_value"
            ],
            set_={
                "can_read": body.can_read,
                "can_write": body.can_write,
                "can_share": body.can_share,
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


@router.patch(
    "/{board_id}/grants/{attr_kind}/{attr_value}",
    response_model=GrantResponse,
)
async def patch_grant(
    board_id: UUID,
    attr_kind: AttrKind,
    attr_value: str,
    body: GrantUpdate,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
    _: None = Depends(verify_token),
) -> BoardGrant:
    """Сменить capability-set у существующего grant'а. Owner/curator only
    (share-delegation не даёт PATCH — только POST read-only invite)."""
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
    if grant is None:
        raise APIError(
            404, "grant_not_found",
            f"Grant {attr_kind}:{value} not found on this board",
        )
    grant.can_read = body.can_read
    grant.can_write = body.can_write
    grant.can_share = body.can_share
    await db.commit()
    await db.refresh(grant)
    return grant


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
    """Owner/curator only (share-delegation не даёт DELETE)."""
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
    """Передать владельца доски. BRD-1 Stage 5.

    Owner-only. Curator — через `/admin/boards/{id}/assign-owner` (Stage 6).

    Target должен уже быть в grants с резолвленным subject_uuid
    (target_must_be_member_first). Транзакция:
    1. boards.owner_uuid → target_uuid;
    2. удаляем grant-строки target (он теперь owner);
    3. вставляем grant для старого owner: {r=t, w=t, s=f} (Q2 из BRD-1;
       can_share=false — старый owner не автоматически получает право
       переприглашать в новой политике).
    """
    board = await _load_board(db, board_id)
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

    if ctx.user_email:
        demoted_attr_kind = "email"
        demoted_attr_value = ctx.user_email
    elif ctx.user_handle:
        demoted_attr_kind = "handle"
        demoted_attr_value = ctx.user_handle
    else:
        raise APIError(
            400,
            "no_demote_attr",
            "Cannot demote old owner: no email or handle in identity",
        )

    ts = now_ms()
    board.owner_uuid = body.target_uuid
    for g in target_grants:
        await db.delete(g)

    stmt = (
        pg_insert(BoardGrant)
        .values(
            board_id=board_id,
            subject_attr_kind=demoted_attr_kind,
            subject_attr_value=demoted_attr_value,
            subject_uuid=old_owner_uuid,
            can_read=True,
            can_write=True,
            can_share=False,
            granted_by_uuid=old_owner_uuid,
            granted_at=ts,
        )
        .on_conflict_do_update(
            index_elements=[
                "board_id", "subject_attr_kind", "subject_attr_value"
            ],
            set_={
                "subject_uuid": old_owner_uuid,
                "can_read": True,
                "can_write": True,
                "can_share": False,
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
