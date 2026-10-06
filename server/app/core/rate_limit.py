"""Rate limiting as a FastAPI dependency, backed by the `limits` library.

Used as a dependency (rather than slowapi's decorator) so it can be attached to routers we
don't own, such as the fastapi-users login/register routes.
"""

import math
import time

from fastapi import Request
from limits import RateLimitItem, parse
from limits.aio.storage import MemoryStorage
from limits.aio.strategies import MovingWindowRateLimiter

from app.core.errors import RateLimited


def build_rate_limiter() -> MovingWindowRateLimiter:
    # In-memory: correct for a single process. Multiple workers/replicas would need a shared
    # backend (e.g. `limits.aio.storage.RedisStorage`) so counts aren't split between them.
    return MovingWindowRateLimiter(MemoryStorage())


async def enforce_rate_limit(request: Request, item: RateLimitItem, scope: str, identifier: str, message: str) -> None:
    """Count one request for `identifier`; raise 429 with Retry-After once over the limit.
    `message` may contain `{seconds}`."""
    limiter: MovingWindowRateLimiter = request.app.state.rate_limiter
    if await limiter.hit(item, scope, identifier):
        return
    stats = await limiter.get_window_stats(item, scope, identifier)
    retry_after = max(1, math.ceil(stats.reset_time - time.time()))
    raise RateLimited(message.format(seconds=retry_after), headers={"Retry-After": str(retry_after)})


class RateLimit:
    """Limits requests per client IP. Usage: `dependencies=[Depends(RateLimit("auth", "10/minute"))]`."""

    def __init__(self, scope: str, limit: str) -> None:
        self.scope = scope
        self.item: RateLimitItem = parse(limit)

    async def __call__(self, request: Request) -> None:
        client = request.client.host if request.client else "unknown"
        await enforce_rate_limit(
            request, self.item, self.scope, client, "Too many attempts. Please wait {seconds} seconds and try again."
        )
