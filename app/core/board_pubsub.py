"""In-memory pubsub для real-time обновлений доски (карта #36).

Topic = `board_id` (UUID). Каждый write-endpoint после commit вызывает
`publish(board_id, event)`. Frontend (vite :5185) подписан на
`GET /api/v1/boards/{id}/events` (SSE).

Single-worker only (current dev-стенд). Если когда-то board будет
multi-worker — менять на pg LISTEN/NOTIFY или Redis.

Format event'а (примеры):
    {"type": "element_upserted", "element": {... ElementResponse ...}, "ts": 1717...}
    {"type": "element_deleted",  "element_id": "uuid", "ts": ...}
    {"type": "element_patched",  "element": {...}, "ts": ...}
    {"type": "board_patched",    "board":   {...}, "ts": ...}
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import AsyncIterator
from uuid import UUID

_subs: dict[UUID, set[asyncio.Queue]] = defaultdict(set)


def publish(board_id: UUID, event: dict) -> None:
    """Broadcast event ко всем подписчикам данной доски.

    Slow-subscriber: dropped (put_nowait) — переподпишется через GET снапшот.
    """
    for q in list(_subs.get(board_id, ())):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def subscribe(board_id: UUID) -> AsyncIterator[dict]:
    """Async generator событий доски. Caller должен `aclose()` при unmount."""
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subs[board_id].add(q)
    try:
        while True:
            yield await q.get()
    finally:
        _subs[board_id].discard(q)
        if not _subs[board_id]:
            _subs.pop(board_id, None)
