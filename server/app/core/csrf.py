"""CSRF protection (double-submit cookie), via starlette-csrf.

Every state-changing request from the browser must echo the `csrftoken` cookie in the
`X-CSRFToken` header. A cross-site attacker can make the browser *send* our cookies, but can't
*read* them to build the header. The public `/api/v1` API is exempt: it authenticates with an
API key header, never with cookies, so there is no ambient credential to forge a request with.
"""

import re

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp
from starlette_csrf import CSRFMiddleware

CSRF_COOKIE_NAME = "csrftoken"
CSRF_HEADER_NAME = "x-csrftoken"


class JSONCSRFMiddleware(CSRFMiddleware):
    def __init__(self, app: ASGIApp, *, secret: str, cookie_secure: bool) -> None:
        super().__init__(
            app,
            secret=secret,
            exempt_urls=[re.compile(r"^/api/v1/")],
            cookie_name=CSRF_COOKIE_NAME,
            header_name=CSRF_HEADER_NAME,
            cookie_secure=cookie_secure,
            cookie_httponly=False,  # the UI must read it to echo it back in the header
            cookie_samesite="strict",
        )

    def _get_error_response(self, request: Request) -> Response:
        # Same JSON shape as every other API error, instead of the library's plain-text body.
        return JSONResponse(
            {"detail": "Your security token is missing or expired. Refresh the page and try again."},
            status_code=403,
        )
