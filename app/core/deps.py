from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.database import get_db
from app.core.exceptions import APIError


async def verify_token(authorization: str | None = Header(default=None)) -> None:
    if not authorization:
        raise APIError(401, "unauthorized", "Missing Authorization header")
    if not authorization.startswith("Bearer "):
        raise APIError(401, "unauthorized", "Invalid authorization format")
    if authorization[7:] != settings.api_key:
        raise APIError(401, "unauthorized", "Invalid token")


__all__ = ["verify_token", "get_db"]
