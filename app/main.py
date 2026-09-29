import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.v1 import router as transcriptions_router
from app.services.storage import get_storage

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        await anyio.to_thread.run_sync(get_storage().ensure_bucket)
    except Exception:
        logger.warning("object storage bucket check failed at startup", exc_info=True)
    yield


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    app = FastAPI(title="Audio Transcription Pipeline", version="0.1.0", lifespan=lifespan)
    app.include_router(transcriptions_router)

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if not isinstance(detail, dict):
            detail = {"code": _default_code(exc.status_code), "message": str(detail)}
        return JSONResponse(status_code=exc.status_code, content={"error": detail})

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first.get("loc", ()))
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": f"{location}: {first.get('msg', 'invalid request')}",
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": {"code": "internal_error", "message": "Internal server error"}},
        )

    return app


def _default_code(status_code: int) -> str:
    return {
        status.HTTP_401_UNAUTHORIZED: "unauthorized",
        status.HTTP_403_FORBIDDEN: "forbidden",
        status.HTTP_404_NOT_FOUND: "not_found",
        status.HTTP_409_CONFLICT: "conflict",
        status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
    }.get(status_code, "error")


app = create_app()
