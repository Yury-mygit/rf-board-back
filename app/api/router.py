from fastapi import APIRouter

from app.api import boards, frames

api_router = APIRouter()
api_router.include_router(boards.router)
api_router.include_router(frames.router)
