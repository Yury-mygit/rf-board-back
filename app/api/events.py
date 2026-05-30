"""SSE-канал live-обновлений доски (карта #36).

Endpoint: `GET /api/v1/boards/{board_id}/events`.

Auth: либо Bearer-token (для CLI/scripts/MCP), либо forward_auth от
Caddy через `auth_session` cookie (для user UI). Bearer проверяется
здесь (settings.all_api_keys); cookie — на уровне Caddy `auth_required
board-dev`, backend ничего не проверяет (доверяет X-User-Email).

Heartbeat 30с, retry 5с. Caddy `board.dev` блок выставляет
`flush_interval -1` для path `/api/v1/boards/*/events`.
"""
from __future__ import annotations

import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from app.core.board_pubsub import subscribe
from app.core.config import settings


router = APIRouter(prefix="/boards", tags=["events"])

_HEARTBEAT_SECONDS = 30


def _has_valid_bearer(authorization: str | None, token: str | None) -> bool:
    raw = None
    if authorization and authorization.startswith("Bearer "):
        raw = authorization[7:]
    elif token:
        raw = token
    return bool(raw) and raw in settings.all_api_keys


@router.get("/{board_id}/events")
async def board_events(
    request: Request,
    board_id: UUID,
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
    x_user_email: str | None = Header(default=None, alias="X-User-Email"),
) -> StreamingResponse:
    # Авторизация: либо Bearer (для CLI), либо forward_auth-cookie
    # (X-User-Email injected by Caddy auth_required board-dev).
    if not _has_valid_bearer(authorization, token) and not x_user_email:
        from app.core.exceptions import APIError
        raise APIError(401, "unauthorized", "Missing auth (Bearer or session cookie)")

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
