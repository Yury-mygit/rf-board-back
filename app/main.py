from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.info import router as info_router
from app.api.router import api_router
from app.core.config import settings
from app.core.exceptions import APIError

app = FastAPI(title=settings.service_name, version=settings.version)
app.include_router(info_router)
app.include_router(api_router, prefix="/api/v1")


@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error, "message": exc.message},
    )
