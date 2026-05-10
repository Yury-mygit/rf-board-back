from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import verify_token
from app.core.exceptions import APIError
from app.core.utils import now_ms
from app.models.models import Board, BoardElement
from app.schemas.board import (
    BoardCreate,
    BoardElementCreate,
    BoardElementPatch,
    BoardElementResponse,
    BoardFull,
    BoardPatch,
    BoardResponse,
)

router = APIRouter(prefix="/boards", tags=["boards"])


@router.get("", response_model=list[BoardResponse])
async def list_boards(
    include_deleted: bool = Query(default=False, alias="includeDeleted"),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_token),
) -> list[Board]:
    q = select(Board)
    if not include_deleted:
        q = q.where(Board.deleted_at.is_(None))
    q = q.order_by(Board.updated_at.desc())
    result = await db.execute(q)
    return list(result.scalars().all())


@router.post("", response_model=BoardResponse, status_code=status.HTTP_201_CREATED)
async def create_board(
    body: BoardCreate,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_token),
) -> Board:
    if await db.get(Board, body.id):
        raise APIError(409, "conflict", f"Board with id '{body.id}' already exists")
    board = Board(**body.model_dump(), deleted_at=None)
    db.add(board)
    await db.commit()
    await db.refresh(board)
    return board


@router.get("/{board_id}", response_model=BoardFull)
async def get_board(
    board_id: UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_token),
) -> dict:
    board = await db.get(Board, board_id)
    if not board or board.deleted_at is not None:
        raise APIError(404, "board_not_found", f"Board with id '{board_id}' does not exist")
    q = (
        select(BoardElement)
        .where(BoardElement.board_id == board_id, BoardElement.deleted_at.is_(None))
        .order_by(BoardElement.z_index.asc())
    )
    elements = (await db.execute(q)).scalars().all()
    return {
        "id": board.id,
        "title": board.title,
        "created_at": board.created_at,
        "updated_at": board.updated_at,
        "deleted_at": board.deleted_at,
        "elements": list(elements),
    }


@router.patch("/{board_id}", response_model=BoardResponse)
async def patch_board(
    board_id: UUID,
    body: BoardPatch,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_token),
) -> Board:
    board = await db.get(Board, board_id)
    if not board or board.deleted_at is not None:
        raise APIError(404, "board_not_found", f"Board with id '{board_id}' does not exist")
    if body.updated_at >= board.updated_at:
        data = body.model_dump(exclude_unset=True)
        updated_at = data.pop("updated_at")
        for key, value in data.items():
            setattr(board, key, value)
        board.updated_at = updated_at
        await db.commit()
        await db.refresh(board)
    return board


@router.delete("/{board_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_board(
    board_id: UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_token),
) -> None:
    board = await db.get(Board, board_id)
    if not board:
        raise APIError(404, "board_not_found", f"Board with id '{board_id}' does not exist")
    ts = now_ms()
    board.deleted_at = ts
    board.updated_at = ts
    await db.commit()


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
    _: None = Depends(verify_token),
) -> BoardElement:
    board = await db.get(Board, board_id)
    if not board or board.deleted_at is not None:
        raise APIError(404, "board_not_found", f"Board with id '{board_id}' does not exist")
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
    return element


@router.patch("/{board_id}/elements/{element_id}", response_model=BoardElementResponse)
async def patch_element(
    board_id: UUID,
    element_id: UUID,
    body: BoardElementPatch,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_token),
) -> BoardElement:
    element = await db.get(BoardElement, element_id)
    if not element or element.deleted_at is not None or element.board_id != board_id:
        raise APIError(404, "element_not_found", f"Element with id '{element_id}' does not exist")
    if body.updated_at >= element.updated_at:
        data = body.model_dump(exclude_unset=True)
        updated_at = data.pop("updated_at")
        if "parent_id" in data and data["parent_id"] is not None:
            await _validate_parent(db, board_id, element_id, data["parent_id"])
        for key, value in data.items():
            setattr(element, key, value)
        element.updated_at = updated_at
        await db.commit()
        await db.refresh(element)
    return element


@router.delete(
    "/{board_id}/elements/{element_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_element(
    board_id: UUID,
    element_id: UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_token),
) -> None:
    element = await db.get(BoardElement, element_id)
    if not element or element.board_id != board_id:
        raise APIError(404, "element_not_found", f"Element with id '{element_id}' does not exist")
    ts = now_ms()
    element.deleted_at = ts
    element.updated_at = ts
    await db.commit()


@router.post(
    "/{board_id}/elements/{element_id}/restore",
    response_model=BoardElementResponse,
)
async def restore_element(
    board_id: UUID,
    element_id: UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_token),
) -> BoardElement:
    board = await db.get(Board, board_id)
    if not board or board.deleted_at is not None:
        raise APIError(404, "board_not_found", f"Board with id '{board_id}' does not exist")
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
    return element
