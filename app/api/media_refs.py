"""Endpoint для media-dev GC: возвращает все asset_id, на которые board ссылается.

Авторизация — shared secret в заголовке `X-Media-GC-Token`. Не за forward_auth
(зовётся изнутри shared docker network: `media_dev_app → board_dev_app:8000`).
"""
from fastapi import APIRouter, Depends, Header
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.exceptions import APIError

router = APIRouter(tags=["media-refs"])


def _verify_gc_token(x_media_gc_token: str | None = Header(default=None)) -> None:
    expected = settings.media_gc_token
    if not expected:
        raise APIError(503, "not_configured", "MEDIA_GC_TOKEN not set")
    if not x_media_gc_token or x_media_gc_token != expected:
        raise APIError(401, "unauthorized", "invalid gc token")


@router.get("/media-refs")
async def media_refs(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_verify_gc_token),
):
    rows = await db.execute(
        text(
            "SELECT DISTINCT attrs->>'asset_id' AS aid "
            "FROM board_elements "
            "WHERE attrs->>'asset_id' IS NOT NULL AND deleted_at IS NULL"
        )
    )
    asset_ids = [r[0] for r in rows.fetchall() if r[0]]
    return {"asset_ids": asset_ids}
