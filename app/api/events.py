"""SSE-канал live-обновлений доски (карта #36).

Endpoint: `GET /api/v1/boards/{board_id}/events`.

Auth: forward_auth от Caddy (`auth_required board-dev`) инжектит
X-User-Uuid/Email/Is-Curator. `current_user` 401-ит без них —
defence-in-depth против прямых server-to-server вызовов в обход
Caddy. ACL: `require_board(id, "read")` (карта 2026-06-23-board-
ownership-and-grants Stage 3.7).

EventSource не может выставить Authorization header, поэтому
Bearer-shared-key здесь не проверяется — auth опирается на cookie
через forward_auth.

Heartbeat 30с, retry 5с. Caddy `board.dev` блок выставляет
`flush_interval -1` для path `/api/v1/boards/*/events`.
"""
from __future__ import annotations

import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_ctx import AuthCtx, current_user, require_board
from app.core.board_pubsub import subscribe
from app.core.database import get_db


router = APIRouter(prefix="/boards", tags=["events"])

_HEARTBEAT_SECONDS = 30


@router.get("/{board_id}/events")
async def board_events(
    request: Request,
    board_id: UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AuthCtx = Depends(current_user),
) -> StreamingResponse:
    await require_board(db, ctx, board_id, "read")

    async def gen():
        yield "retry: 5000\n\n"
        sub = subscribe(board_id)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(
                        sub.__anext__(), timeout=_HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event, default=str)}\n\n"
        finally:
            await sub.aclose()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
