"""FastMCP server exposing board tools over /mcp.

Тонкий wrapper над `/api/v1/boards/*` через httpx-loopback на 127.0.0.1:8000
со static API_KEY. Аналог `docs.dev/mcp` (см. карта
`cards/board/feature/2026-05-30-board-mcp-server.md`).

Авторизация: за `auth_required board-dev` в Caddy ходит юзер с api_token
grant'ом board-dev; Caddy подменяет Authorization на статичный API_KEY
(snippet `inject_api_key_upstream`).
"""
from typing import Any

import httpx
from fastmcp import FastMCP

from app.core.config import settings

mcp = FastMCP("board")

INTERNAL_BASE = "http://127.0.0.1:8000/api/v1"
PUBLIC_FRAME_BASE = "https://board.dev.raftforge.art/api/v1/frames"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=INTERNAL_BASE,
        headers={"Authorization": f"Bearer {settings.api_key}"},
        timeout=10.0,
    )


@mcp.tool
async def board_list_boards(include_deleted: bool = False) -> list[dict]:
    """List all boards (id, title, order_index, timestamps). By default
    excludes soft-deleted; pass include_deleted=True to see them."""
    async with _client() as c:
        r = await c.get("/boards", params={"includeDeleted": include_deleted})
        r.raise_for_status()
        return r.json()


@mcp.tool
async def board_get(board_id: str) -> dict:
    """Read a single board with ALL its elements (frames, shapes, etc).
    Returns the board metadata + `elements` array sorted by z-index."""
    async with _client() as c:
        r = await c.get(f"/boards/{board_id}")
        r.raise_for_status()
        return r.json()


@mcp.tool
async def board_list_elements(board_id: str, type: str | None = None) -> list[dict]:
    """List elements of a board, optionally filtered by `type` (e.g.
    'frame', 'shape'). Convenience wrapper around board_get — returns
    only the `elements` array (filtered)."""
    async with _client() as c:
        r = await c.get(f"/boards/{board_id}")
        r.raise_for_status()
        elements = r.json().get("elements", [])
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
    body = {
        "id": id,
        "external_ref": external_ref,
        "type": type,
        "parent_id": parent_id,
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "attrs": attrs,
        "created_at": created_at,
        "updated_at": updated_at,
    }
    async with _client() as c:
        r = await c.post(f"/boards/{board_id}/elements/by-ref", json=body)
        r.raise_for_status()
        return r.json()


@mcp.tool
async def board_delete_element_by_ref(board_id: str, external_ref: str) -> dict:
    """Soft-delete an element by its `external_ref` on the given board."""
    async with _client() as c:
        r = await c.delete(f"/boards/{board_id}/elements/by-ref/{external_ref}")
        r.raise_for_status()
    return {"ok": True}


@mcp.tool
async def board_get_frame_url(frame_id: str, fmt: str = "png") -> str:
    """Return a public URL for a frame's HTML or PNG render. `fmt` ∈
    {'png', 'html'}. The URL is unauthenticated (frame-share is public)
    so it can be opened directly or fetched without bearer."""
    if fmt not in {"png", "html"}:
        raise ValueError("fmt must be 'png' or 'html'")
    return f"{PUBLIC_FRAME_BASE}/{frame_id}.{fmt}"


mcp_http_app = mcp.http_app(path="/")
