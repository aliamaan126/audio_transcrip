import logging
import time
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

import redis.asyncio as redis
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import async_session

logger = logging.getLogger(__name__)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def api_error(status_code: int, code: str, message: str) -> HTTPException:
    """Build an exception rendered by the shared error handler."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


@lru_cache(maxsize=1)
def _redis_client() -> redis.Redis:
    return redis.from_url(get_settings().redis_url, decode_responses=True)


async def get_db() -> AsyncIterator[AsyncSession]:
    async for session in async_session():
        yield session


async def require_api_key(key: str | None = Security(_api_key_header)) -> str:
    """Authenticate the caller and apply the per-key request budget."""
    settings = get_settings()
    if not key or key not in settings.api_keys:
        raise api_error(status.HTTP_401_UNAUTHORIZED, "unauthorized", "Missing or invalid API key")
    await _enforce_rate_limit(key, settings)
    return key


async def _enforce_rate_limit(key: str, settings: Settings) -> None:
    if settings.rate_limit_requests <= 0:
        return

    window = settings.rate_limit_window_seconds
    bucket = f"ratelimit:{key}:{int(time.time()) // window}"
    try:
        client = _redis_client()
        used = await client.incr(bucket)
        if used == 1:
            await client.expire(bucket, window)
    except redis.RedisError:
        # A rate limiter outage must not take the API down.
        logger.warning("rate limiting unavailable, allowing request", exc_info=True)
        return

    if used > settings.rate_limit_requests:
        raise api_error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate_limited",
            f"Limit of {settings.rate_limit_requests} requests per {window}s exceeded",
        )


DbSession = Annotated[AsyncSession, Depends(get_db)]
ApiKey = Annotated[str, Depends(require_api_key)]
