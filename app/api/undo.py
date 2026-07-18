"""BRD-19: undo/redo endpoints.

Публичные:
- `POST /boards/{id}/undo` — откатить последнее (свое) действие в стеке
  γ2 (см. BRD-13 модель).
- `POST /boards/{id}/redo` — вернуть последний undone.
- `GET /boards/{id}/undo/state` — {canUndo, canRedo, next_*_desc} для
  current user.

Мутации по элементам выполняются напрямую в БД через `undo_engine`,
без вызовов `create_element`/`patch_element`/`delete_element` — иначе
`record_action` из BRD-18 запишет откат как новый action.

SSE broadcast — существующий `element_*` payload с sidecar `undo_state`
для всех associated_users.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_ctx import AuthCtx, current_user, require_board
from app.core.board_pubsub import publish as bp_publish
from app.core.database import get_db
from app.core.undo_engine import (
    apply_redo,
    apply_undo,
    compute_undo_state,
    compute_undo_state_map,
    pop_redoable,
    pop_undoable,
)


router = APIRouter(prefix="/boards", tags=["undo"])


@router.post("/{board_id}/undo")
async def undo(
    board_id: UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> dict:
    await require_board(db, ctx, board_id, "write")
    action = await pop_undoable(db, board_id=board_id, user_uuid=ctx.user_uuid)
    if action is None:
        return {
            "undo_state": await compute_undo_state(
                db, board_id=board_id, user_uuid=ctx.user_uuid
            ),
        }

    payload = await apply_undo(db, action)
    action.undone = True
    await db.commit()

    if payload is not None:
        state_map = await compute_undo_state_map(
            db, board_id=board_id, user_uuids=list(action.associated_users or [])
        )
        payload["undo_state"] = state_map
        bp_publish(board_id, payload)

    return {
        "undo_state": await compute_undo_state(
            db, board_id=board_id, user_uuid=ctx.user_uuid
        ),
    }


@router.post("/{board_id}/redo")
async def redo(
    board_id: UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> dict:
    await require_board(db, ctx, board_id, "write")
    action = await pop_redoable(db, board_id=board_id, user_uuid=ctx.user_uuid)
    if action is None:
        return {
            "undo_state": await compute_undo_state(
                db, board_id=board_id, user_uuid=ctx.user_uuid
            ),
        }

    payload = await apply_redo(db, action)
    action.undone = False
    await db.commit()

    if payload is not None:
        state_map = await compute_undo_state_map(
            db, board_id=board_id, user_uuids=list(action.associated_users or [])
        )
        payload["undo_state"] = state_map
        bp_publish(board_id, payload)

    return {
        "undo_state": await compute_undo_state(
            db, board_id=board_id, user_uuid=ctx.user_uuid
        ),
    }


@router.get("/{board_id}/undo/state")
async def undo_state(
    board_id: UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> dict:
    await require_board(db, ctx, board_id, "read")
    state = await compute_undo_state(
        db, board_id=board_id, user_uuid=ctx.user_uuid
    )
    # BRD-20: клиент использует my_uuid чтобы фильтровать SSE undo_state map.
    state["my_uuid"] = str(ctx.user_uuid)
    return state
