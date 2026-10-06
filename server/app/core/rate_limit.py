"""Rate limiting as a FastAPI dependency, backed by the `limits` library.

Used as a dependency (rather than slowapi's decorator) so it can be attached to routers we
don't own, such as the fastapi-users login/register routes.
"""

import math
import time

from fastapi import HTTPException, Request, status
from limits import RateLimitItem, parse
from limits.aio.storage import MemoryStorage
from limits.aio.strategies import MovingWindowRateLimiter


def build_rate_limiter() -> MovingWindowRateLimiter:
    # In-memory: correct for a single process. Multiple workers/replicas would need a shared
    # backend (e.g. `limits.aio.storage.RedisStorage`) so counts aren't split between them.
    return MovingWindowRateLimiter(MemoryStorage())


class RateLimit:
    """Limits requests per client IP. Usage: `dependencies=[Depends(RateLimit("auth", "10/minute"))]`."""

    def __init__(self, scope: str, limit: str) -> None:
        self.scope = scope
        self.item: RateLimitItem = parse(limit)

    async def __call__(self, request: Request) -> None:
        limiter: MovingWindowRateLimiter = request.app.state.rate_limiter
        client = request.client.host if request.client else "unknown"
        if await limiter.hit(self.item, self.scope, client):
            return
        stats = await limiter.get_window_stats(self.item, self.scope, client)
        retry_after = max(1, math.ceil(stats.reset_time - time.time()))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many attempts. Please wait {retry_after} seconds and try again.",
            headers={"Retry-After": str(retry_after)},
        )
