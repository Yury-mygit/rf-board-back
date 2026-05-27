from fastapi import APIRouter

from app.api import boards, frames, media_refs

api_router = APIRouter()
api_router.include_router(boards.router)
api_router.include_router(frames.router)
api_router.include_router(media_refs.router)
