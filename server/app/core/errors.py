"""Application errors with a user-facing message and a stable machine-readable code.

Raised from services and dependencies; one exception handler (see `app.main`) renders them all as
`{"detail": "...", "code": "..."}`, so UI and API clients get the same shape everywhere.
"""

from fastapi import status


class AppError(Exception):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "error"
    message = "Something went wrong. Please try again."

    def __init__(self, message: str | None = None, *, headers: dict[str, str] | None = None) -> None:
        self.message = message or self.message
        self.headers = headers
        super().__init__(self.message)


class RateLimited(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many requests. Please wait a moment and try again."
