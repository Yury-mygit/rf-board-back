from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_ctx import (
    AuthCtx,
    BoardCaps,
    current_user,
    require_board,
    visible_boards_query,
    your_capabilities_map,
)
from app.core.board_pubsub import publish as bp_publish
from app.core.database import get_db
from app.core.exceptions import APIError
from app.core.utils import now_ms
from app.models.models import Board, BoardElement
from app.schemas.board import (
    BoardCreate,
    BoardElementCreate,
    BoardElementPatch,
    BoardElementResponse,
    BoardElementUpsertByRef,
    BoardFull,
    BoardPatch,
    BoardResponse,
)


async def _move_children(
    db: AsyncSession, board_id: UUID, parent_id: UUID,
    dx: float, dy: float, ts: int,
) -> list[BoardElement]:
    """Рекурсивно сдвигает всех потомков (frame и dx/dy) — см. карта
    `cards/board/bug/2026-05-30-frame-move-no-cascade-to-children.md`.

    Возвращает плоский список перемещённых элементов (для emit publish).
    """
    moved: list[BoardElement] = []
    children = (
        await db.execute(
            select(BoardElement).where(
                BoardElement.board_id == board_id,
                BoardElement.parent_id == parent_id,
                BoardElement.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    for c in children:
        c.x = c.x + dx
        c.y = c.y + dy
        c.updated_at = ts
        moved.append(c)
        # рекурсия: если ребёнок — frame, его потомки тоже двигаются
        if c.type == "frame":
            moved.extend(await _move_children(db, board_id, c.id, dx, dy, ts))
    return moved


def _el_payload(el: BoardElement) -> dict:
    """Сериализация элемента для SSE payload (плоский dict)."""
    return {
        "id": str(el.id),
        "board_id": str(el.board_id),
        "type": el.type,
        "external_ref": str(el.external_ref) if el.external_ref else None,
        "parent_id": str(el.parent_id) if el.parent_id else None,
        "z_index": el.z_index,
        "x": el.x, "y": el.y, "w": el.w, "h": el.h,
        "attrs": el.attrs or {},
        "created_at": el.created_at, "updated_at": el.updated_at,
        "deleted_at": el.deleted_at,
    }

router = APIRouter(prefix="/boards", tags=["boards"])


_NO_CAPS = BoardCaps()


def _board_dict(b: Board, caps: BoardCaps | None) -> dict:
    """BRD-3: единый сериализатор Board → dict с capability-флагами."""
    c = caps or _NO_CAPS
    return {
        "id": b.id,
        "title": b.title,
        "order_index": b.order_index,
        "owner_uuid": b.owner_uuid,
        "is_owner": c.is_owner,
        "is_curator": c.is_curator,
        "can_read": c.can_read,
        "can_write": c.can_write,
        "can_share": c.can_share,
        "created_at": b.created_at,
        "updated_at": b.updated_at,
        "deleted_at": b.deleted_at,
    }


@router.get("", response_model=list[BoardResponse])
async def list_boards(
    include_deleted: bool = Query(default=False, alias="includeDeleted"),
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> list[dict]:
    q = visible_boards_query(ctx)
    if not include_deleted:
        q = q.where(Board.deleted_at.is_(None))
    q = q.order_by(Board.order_index.asc(), Board.updated_at.desc())
    boards = list((await db.execute(q)).scalars().all())
    caps = await your_capabilities_map(db, ctx, boards)
    return [_board_dict(b, caps.get(b.id)) for b in boards]


@router.post("", response_model=BoardResponse, status_code=status.HTTP_201_CREATED)
async def create_board(
    body: BoardCreate,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> dict:
    if await db.get(Board, body.id):
        raise APIError(409, "conflict", f"Board with id '{body.id}' already exists")
    board = Board(**body.model_dump(), deleted_at=None, owner_uuid=ctx.user_uuid)
    db.add(board)
    await db.commit()
    await db.refresh(board)
    bp_publish(board.id, {
        "type": "board_created",
        "board": {
            "id": str(board.id), "title": board.title,
            "created_at": board.created_at, "updated_at": board.updated_at,
        },
    })
    # Создатель → owner (или curator, если так). Всё разрешено.
    caps = BoardCaps(
        is_owner=not ctx.is_curator, is_curator=ctx.is_curator,
        can_read=True, can_write=True, can_share=True,
    )
    return _board_dict(board, caps)


@router.get("/{board_id}", response_model=BoardFull)
async def get_board(
    board_id: UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> dict:
    board = await require_board(db, ctx, board_id, "read")
    q = (
        select(BoardElement)
        .where(BoardElement.board_id == board_id, BoardElement.deleted_at.is_(None))
        .order_by(BoardElement.z_index.asc())
    )
    elements = (await db.execute(q)).scalars().all()
    caps = await your_capabilities_map(db, ctx, [board])
    return {
        **_board_dict(board, caps.get(board.id)),
        "elements": list(elements),
    }


@router.patch("/{board_id}", response_model=BoardResponse)
async def patch_board(
    board_id: UUID,
    body: BoardPatch,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> dict:
    board = await require_board(db, ctx, board_id, "write")
    if body.updated_at >= board.updated_at:
        data = body.model_dump(exclude_unset=True)
        updated_at = data.pop("updated_at")
        for key, value in data.items():
            setattr(board, key, value)
        board.updated_at = updated_at
        await db.commit()
        await db.refresh(board)
        bp_publish(board.id, {
            "type": "board_patched",
            "board": {
                "id": str(board.id), "title": board.title,
                "created_at": board.created_at, "updated_at": board.updated_at,
            },
        })
    caps = await your_capabilities_map(db, ctx, [board])
    return _board_dict(board, caps.get(board.id))


@router.delete("/{board_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_board(
    board_id: UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> None:
    board = await db.get(Board, board_id)
    if not board:
        raise APIError(404, "board_not_found", f"Board with id '{board_id}' does not exist")
    if not ctx.is_curator and board.owner_uuid != ctx.user_uuid:
        raise APIError(403, "forbidden", "Only board owner or curator can delete")
    live_count = (
        await db.execute(
            select(func.count(BoardElement.id)).where(
                BoardElement.board_id == board_id,
                BoardElement.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    if live_count > 0:
        raise APIError(
            409,
            "board_not_empty",
            f"Board has {live_count} live element(s); delete them first",
        )
    ts = now_ms()
    board.deleted_at = ts
    board.updated_at = ts
    await db.commit()
    bp_publish(board_id, {"type": "board_deleted", "board_id": str(board_id), "ts": ts})


# ── Elements ──────────────────────────────────────────────────────────────────


async def _validate_parent(
    db: AsyncSession, board_id: UUID, element_id: UUID, parent_id: UUID
) -> None:
    if parent_id == element_id:
        raise APIError(400, "invalid_parent", "Element cannot be its own parent")
    parent = await db.get(BoardElement, parent_id)
    if (
        not parent
        or parent.deleted_at is not None
        or parent.board_id != board_id
        or parent.type != "frame"
    ):
        raise APIError(400, "invalid_parent", f"Parent '{parent_id}' is not a frame in this board")


@router.post(
    "/{board_id}/elements",
    response_model=BoardElementResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_element(
    board_id: UUID,
    body: BoardElementCreate,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> BoardElement:
    await require_board(db, ctx, board_id, "write")
    if await db.get(BoardElement, body.id):
        raise APIError(409, "conflict", f"Element with id '{body.id}' already exists")
    if body.parent_id is not None:
        await _validate_parent(db, board_id, body.id, body.parent_id)
    max_z = (
        await db.execute(
            select(func.max(BoardElement.z_index)).where(BoardElement.board_id == board_id)
        )
    ).scalar()
    next_z = (max_z + 1) if max_z is not None else 0
    element = BoardElement(
        **body.model_dump(),
        board_id=board_id,
        z_index=next_z,
        deleted_at=None,
    )
    db.add(element)
    await db.commit()
    await db.refresh(element)
    bp_publish(board_id, {"type": "element_upserted", "element": _el_payload(element)})
    return element


@router.patch("/{board_id}/elements/{element_id}", response_model=BoardElementResponse)
async def patch_element(
    board_id: UUID,
    element_id: UUID,
    body: BoardElementPatch,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> BoardElement:
    await require_board(db, ctx, board_id, "write")
    element = await db.get(BoardElement, element_id)
    if not element or element.deleted_at is not None or element.board_id != board_id:
        raise APIError(404, "element_not_found", f"Element with id '{element_id}' does not exist")
    if body.updated_at >= element.updated_at:
        data = body.model_dump(exclude_unset=True)
        updated_at = data.pop("updated_at")
        if "parent_id" in data and data["parent_id"] is not None:
            await _validate_parent(db, board_id, element_id, data["parent_id"])
        # cascade-move для frame: запоминаем старые координаты
        old_x, old_y = element.x, element.y
        for key, value in data.items():
            setattr(element, key, value)
        element.updated_at = updated_at
        cascade_dx = cascade_dy = 0.0
        if element.type == "frame":
            cascade_dx = element.x - old_x
            cascade_dy = element.y - old_y
            if cascade_dx or cascade_dy:
                # БД-cascade: чтобы reload показывал согласованное состояние.
                # Events для children НЕ публикуем — фронт сам translate'нет
                # всех потомков локально (одна синхронная анимация).
                await _move_children(
                    db, board_id, element_id, cascade_dx, cascade_dy, updated_at,
                )
        await db.commit()
        await db.refresh(element)
        payload = _el_payload(element)
        if cascade_dx or cascade_dy:
            payload["cascade_dx"] = cascade_dx
            payload["cascade_dy"] = cascade_dy
        bp_publish(board_id, {"type": "element_patched", "element": payload})
    return element


@router.delete(
    "/{board_id}/elements/{element_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_element(
    board_id: UUID,
    element_id: UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> None:
    await require_board(db, ctx, board_id, "write")
    element = await db.get(BoardElement, element_id)
    if not element or element.board_id != board_id:
        raise APIError(404, "element_not_found", f"Element with id '{element_id}' does not exist")
    ts = now_ms()
    element.deleted_at = ts
    element.updated_at = ts
    await db.commit()


# ── Upsert / lookup / delete по external_ref ──────────────────────────────────
# Используется auto_designer (Python pkg + CLI) для повторяемого рисования
# фреймов: каждый screen имеет стабильный `external_ref` (UUID), скрипт
# делает upsert по нему, internal `id` сохраняется.
# Карта: cards/board/feature/2026-05-30-board-external-ref-stable-id.md.


@router.post(
    "/{board_id}/elements/by-ref",
    response_model=BoardElementResponse,
)
async def upsert_element_by_ref(
    board_id: UUID,
    body: BoardElementUpsertByRef,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> BoardElement:
    await require_board(db, ctx, board_id, "write")

    existing = (
        await db.execute(
            select(BoardElement).where(
                BoardElement.board_id == board_id,
                BoardElement.external_ref == body.external_ref,
                BoardElement.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        # UPDATE: id не меняем (PK + потенциальные FK). z_index сохраняем.
        if body.parent_id is not None:
            await _validate_parent(db, board_id, existing.id, body.parent_id)
        # cascade-move для frame: запоминаем старые координаты
        old_x, old_y = existing.x, existing.y
        existing.type = body.type
        existing.parent_id = body.parent_id
        existing.x = body.x
        existing.y = body.y
        existing.w = body.w
        existing.h = body.h
        existing.attrs = body.attrs
        existing.updated_at = body.updated_at
        cascade_dx = cascade_dy = 0.0
        if existing.type == "frame":
            cascade_dx = body.x - old_x
            cascade_dy = body.y - old_y
            if cascade_dx or cascade_dy:
                await _move_children(
                    db, board_id, existing.id, cascade_dx, cascade_dy, body.updated_at,
                )
        await db.commit()
        await db.refresh(existing)
        payload = _el_payload(existing)
        if cascade_dx or cascade_dy:
            payload["cascade_dx"] = cascade_dx
            payload["cascade_dy"] = cascade_dy
        bp_publish(board_id, {"type": "element_upserted", "element": payload})
        return existing

    # INSERT: новый элемент с переданным `id` и `external_ref`.
    if await db.get(BoardElement, body.id):
        raise APIError(
            409,
            "conflict",
            f"Element with id '{body.id}' already exists (but with different external_ref)",
        )
    if body.parent_id is not None:
        await _validate_parent(db, board_id, body.id, body.parent_id)
    max_z = (
        await db.execute(
            select(func.max(BoardElement.z_index)).where(BoardElement.board_id == board_id)
        )
    ).scalar()
    next_z = (max_z + 1) if max_z is not None else 0
    element = BoardElement(
        id=body.id,
        board_id=board_id,
        external_ref=body.external_ref,
        type=body.type,
        parent_id=body.parent_id,
        z_index=next_z,
        x=body.x,
        y=body.y,
        w=body.w,
        h=body.h,
        attrs=body.attrs,
        created_at=body.created_at,
        updated_at=body.updated_at,
        deleted_at=None,
    )
    db.add(element)
    await db.commit()
    await db.refresh(element)
    bp_publish(board_id, {"type": "element_upserted", "element": _el_payload(element)})
    return element


@router.get(
    "/{board_id}/elements/by-ref/{external_ref}",
    response_model=BoardElementResponse,
)
async def get_element_by_ref(
    board_id: UUID,
    external_ref: UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> BoardElement:
    await require_board(db, ctx, board_id, "read")
    element = (
        await db.execute(
            select(BoardElement).where(
                BoardElement.board_id == board_id,
                BoardElement.external_ref == external_ref,
                BoardElement.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if element is None:
        raise APIError(
            404,
            "element_not_found",
            f"Element with external_ref '{external_ref}' does not exist in board '{board_id}'",
        )
    return element


@router.delete(
    "/{board_id}/elements/by-ref/{external_ref}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_element_by_ref(
    board_id: UUID,
    external_ref: UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> None:
    await require_board(db, ctx, board_id, "write")
    element = (
        await db.execute(
            select(BoardElement).where(
                BoardElement.board_id == board_id,
                BoardElement.external_ref == external_ref,
                BoardElement.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if element is None:
        raise APIError(
            404,
            "element_not_found",
            f"Element with external_ref '{external_ref}' does not exist in board '{board_id}'",
        )
    ts = now_ms()
    element.deleted_at = ts
    element.updated_at = ts
    await db.commit()
    bp_publish(board_id, {
        "type": "element_deleted",
        "element_id": str(element.id),
        "external_ref": str(external_ref),
        "ts": ts,
    })


@router.post(
    "/{board_id}/elements/{element_id}/restore",
    response_model=BoardElementResponse,
)
async def restore_element(
    board_id: UUID,
    element_id: UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> BoardElement:
    await require_board(db, ctx, board_id, "write")
    element = await db.get(BoardElement, element_id)
    if not element or element.board_id != board_id:
        raise APIError(404, "element_not_found", f"Element with id '{element_id}' does not exist")
    if element.deleted_at is None:
        raise APIError(400, "not_deleted", f"Element with id '{element_id}' is not deleted")
    if element.parent_id is not None:
        await _validate_parent(db, board_id, element_id, element.parent_id)
    ts = now_ms()
    element.deleted_at = None
    element.updated_at = ts
    await db.commit()
    await db.refresh(element)
    bp_publish(board_id, {"type": "element_upserted", "element": _el_payload(element)})
    return element
