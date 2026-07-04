from fastapi import APIRouter

from app.api import admin, boards, events, frames, grants, media_refs

api_router = APIRouter()
api_router.include_router(boards.router)
api_router.include_router(frames.router)
api_router.include_router(media_refs.router)
api_router.include_router(events.router)
api_router.include_router(grants.router)
api_router.include_router(admin.router)
