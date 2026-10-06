from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from app.api import auth, health, jira
from app.core.config import get_settings
from app.core.csrf import JSONCSRFMiddleware
from app.core.rate_limit import build_rate_limiter
from app.core.security_headers import SecurityHeadersMiddleware
from app.db.session import close_db, init_db
from app.jira.errors import JiraError
from app.jira.oauth import build_oauth


class SPAStaticFiles(StaticFiles):
    """Serves the built React app, falling back to index.html so client-side routes
    (e.g. /settings) work on a hard refresh."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # Only page routes fall back. Unknown API paths and missing assets must stay 404s,
            # otherwise API clients would receive HTML with a 200.
            is_page_route = not path.startswith("api/") and "." not in path.rsplit("/", 1)[-1]
            if exc.status_code != 404 or not is_page_route:
                raise
            return await super().get_response("index.html", scope)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_db()
    yield
    await close_db()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="IdentityHub", version="0.1.0", lifespan=lifespan)
    app.state.rate_limiter = build_rate_limiter()
    app.state.oauth = build_oauth(settings)

    # Middleware added last runs first: security headers wrap everything, including CSRF rejections.
    # Short-lived signed cookie holding OAuth `state` between /jira/connect and /jira/callback.
    # SameSite=Lax so it is sent on the top-level redirect back from Atlassian.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key.get_secret_value(),
        session_cookie="identityhub_oauth",
        max_age=10 * 60,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    app.add_middleware(
        JSONCSRFMiddleware,
        secret=settings.secret_key.get_secret_value(),
        cookie_secure=settings.cookie_secure,
    )
    app.add_middleware(SecurityHeadersMiddleware, https=settings.cookie_secure)

    app.include_router(health.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(jira.router, prefix="/api")

    @app.exception_handler(JiraError)
    async def jira_error_handler(request: Request, exc: JiraError) -> JSONResponse:
        return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=exc.status_code)

    # Mounted last so /api routes take precedence.
    if settings.static_dir and settings.static_dir.is_dir():
        app.mount("/", SPAStaticFiles(directory=settings.static_dir, html=True), name="spa")

    return app


app = create_app()
