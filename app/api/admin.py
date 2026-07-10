"""Curator-only endpoints для orphan-досок.

Карта #130 Stage 6. Orphan = `boards.owner_uuid IS NULL` (доски,
созданные до миграции ownership). Curator (`X-User-Is-Curator: 1`)
видит их через `GET /admin/boards/orphans` и назначает владельца
через `POST /admin/boards/{id}/assign-owner {userUuid}`.

Для not-orphan досок передача владельца идёт через
`POST /boards/{id}/transfer` (owner-only, см. grants.py Stage 5).
"""
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_ctx import AuthCtx, current_user
from app.core.database import get_db
from app.core.exceptions import APIError
from app.models.models import Board
from app.schemas.board import BoardResponse
from app.schemas.common import CamelModel


router = APIRouter(prefix="/admin", tags=["admin"])


class AssignOwnerRequest(CamelModel):
    user_uuid: UUID


def _require_curator(ctx: AuthCtx) -> None:
    if not ctx.is_curator:
        raise APIError(403, "forbidden", "Curator only")


@router.get("/boards/orphans", response_model=list[BoardResponse])
async def list_orphan_boards(
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> list[Board]:
    _require_curator(ctx)
    rows = await db.execute(
        select(Board)
        .where(Board.owner_uuid.is_(None), Board.deleted_at.is_(None))
        .order_by(Board.created_at.desc())
    )
    return list(rows.scalars().all())


@router.post(
    "/boards/{board_id}/assign-owner",
    response_model=BoardResponse,
)
async def assign_owner(
    board_id: UUID,
    body: AssignOwnerRequest,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> Board:
    """Curator назначает владельца для orphan-доски.

    Только для досок без владельца. Для смены владельца у not-orphan
    использовать `POST /boards/{id}/transfer` (owner-only).
    """
    _require_curator(ctx)
    board = await db.get(Board, board_id)
    if not board or board.deleted_at is not None:
        raise APIError(
            404, "board_not_found", f"Board with id '{board_id}' does not exist"
        )
    if board.owner_uuid is not None:
        raise APIError(
            400,
            "board_not_orphan",
            "Board already has an owner; use /boards/{id}/transfer to change",
        )
    board.owner_uuid = body.user_uuid
    await db.commit()
    await db.refresh(board)
    return board
