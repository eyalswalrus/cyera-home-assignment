from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import health
from app.core.config import get_settings
from app.db.session import init_db


class SPAStaticFiles(StaticFiles):
    """Serves the built React app, falling back to index.html so client-side routes
    (e.g. /settings) work on a hard refresh."""

    async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def]
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


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="IdentityHub", version="0.1.0", lifespan=lifespan)

    app.include_router(health.router, prefix="/api")

    # Mounted last so /api routes take precedence.
    if settings.static_dir and settings.static_dir.is_dir():
        app.mount("/", SPAStaticFiles(directory=settings.static_dir, html=True), name="spa")

    return app


app = create_app()
