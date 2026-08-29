"""FastMCP server exposing board tools over /mcp.

Identity-first: Caddy `auth_required board-dev` валидирует Bearer через
auth-service и прокидывает `X-User-{Uuid,Email,Telegram,Handle,Is-Curator}`.
Handler читает headers через get_http_headers(), строит AuthCtx и
вызывает функции роутера `app/api/boards.py` напрямую с открытой
AsyncSessionLocal-сессией — real user_uuid в ACL, без httpx-loopback
и без static API_KEY.

Паттерн — по образцу tasks-MCP (см. per-project/tasks.md, #138).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

from app.api.boards import (
    delete_element_by_ref as _api_delete_element_by_ref,
    get_board as _api_get_board,
    list_boards as _api_list_boards,
    upsert_element_by_ref as _api_upsert_element_by_ref,
)
from app.core.auth_ctx import AuthCtx
from app.core.database import AsyncSessionLocal
from app.core.exceptions import APIError
from app.schemas.board import (
    BoardElementResponse,
    BoardElementUpsertByRef,
    BoardFull,
    BoardResponse,
)

mcp = FastMCP("board")

PUBLIC_FRAME_BASE = "https://board.dev.raftforge.art/api/v1/frames"


def _build_auth_ctx() -> AuthCtx:
    headers = get_http_headers()
    x_user_uuid = headers.get("x-user-uuid")
    if not x_user_uuid:
        raise ValueError("missing X-User-Uuid (request bypassed edge auth)")
    try:
        user_uuid = UUID(x_user_uuid)
    except ValueError:
        raise ValueError("malformed X-User-Uuid")
    return AuthCtx(
        user_uuid=user_uuid,
        user_email=(headers.get("x-user-email") or "").strip().lower(),
        user_telegram=(headers.get("x-user-telegram") or "").strip(),
        user_handle=(headers.get("x-user-handle") or "").strip().lower(),
        is_curator=headers.get("x-user-is-curator") == "1",
    )


@mcp.tool
async def board_list_boards(include_deleted: bool = False) -> list[dict]:
    """List all boards visible to caller (owner or grant). By default
    excludes soft-deleted; pass include_deleted=True to see them."""
    ctx = _build_auth_ctx()
    try:
        async with AsyncSessionLocal() as db:
            raw = await _api_list_boards(include_deleted=include_deleted, db=db, ctx=ctx)
            return [BoardResponse.model_validate(d).model_dump(mode="json", by_alias=True) for d in raw]
    except APIError as e:
        raise ValueError(f"{e.error}: {e.message}") from e


@mcp.tool
async def board_get(board_id: str) -> dict:
    """Read a single board with ALL its elements (frames, shapes, etc).
    Returns the board metadata + `elements` array sorted by z-index."""
    ctx = _build_auth_ctx()
    try:
        async with AsyncSessionLocal() as db:
            raw = await _api_get_board(board_id=UUID(board_id), db=db, ctx=ctx)
            return BoardFull.model_validate(raw).model_dump(mode="json", by_alias=True)
    except APIError as e:
        raise ValueError(f"{e.error}: {e.message}") from e


@mcp.tool
async def board_list_elements(board_id: str, type: str | None = None) -> list[dict]:
    """List elements of a board, optionally filtered by `type` (e.g.
    'frame', 'shape'). Convenience wrapper around board_get."""
    board = await board_get(board_id)
    elements = board.get("elements", [])
    if type is not None:
        elements = [el for el in elements if el.get("type") == type]
    return elements


@mcp.tool
async def board_upsert_element_by_ref(
    board_id: str,
    id: str,
    external_ref: str,
    type: str,
    x: float,
    y: float,
    w: float,
    h: float,
    attrs: dict[str, Any],
    created_at: int,
    updated_at: int,
    parent_id: str | None = None,
) -> dict:
    """Create-or-update an element by `external_ref` (stable cross-run ID).
    On INSERT — the passed `id` is used as the internal PK; on UPDATE —
    the existing element's id is preserved and (type, parent_id, geometry,
    attrs, updated_at) are overwritten. For frames a position delta is
    cascaded to children. Timestamps are ms-epoch ints."""
    ctx = _build_auth_ctx()
    body = BoardElementUpsertByRef(
        id=UUID(id),
        external_ref=UUID(external_ref),
        type=type,
        parent_id=UUID(parent_id) if parent_id else None,
        x=x, y=y, w=w, h=h,
        attrs=attrs,
        created_at=created_at,
        updated_at=updated_at,
    )
    try:
        async with AsyncSessionLocal() as db:
            elem = await _api_upsert_element_by_ref(
                board_id=UUID(board_id), body=body, db=db, ctx=ctx,
            )
            return BoardElementResponse.model_validate(elem).model_dump(mode="json", by_alias=True)
    except APIError as e:
        raise ValueError(f"{e.error}: {e.message}") from e


@mcp.tool
async def board_delete_element_by_ref(board_id: str, external_ref: str) -> dict:
    """Soft-delete an element by its `external_ref` on the given board."""
    ctx = _build_auth_ctx()
    try:
        async with AsyncSessionLocal() as db:
            await _api_delete_element_by_ref(
                board_id=UUID(board_id),
                external_ref=UUID(external_ref),
                db=db, ctx=ctx,
            )
            return {"ok": True}
    except APIError as e:
        raise ValueError(f"{e.error}: {e.message}") from e


@mcp.tool
async def board_get_frame_url(frame_id: str, fmt: str = "png") -> str:
    """Return a public URL for a frame's HTML or PNG render. `fmt` ∈
    {'png', 'html'}. The URL is unauthenticated (frame-share is public)
    so it can be opened directly or fetched without bearer."""
    if fmt not in {"png", "html"}:
        raise ValueError("fmt must be 'png' or 'html'")
    return f"{PUBLIC_FRAME_BASE}/{frame_id}.{fmt}"


mcp_http_app = mcp.http_app(path="/")
