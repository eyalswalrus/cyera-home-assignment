"""A reference for the public REST API only (`/api/v1`), separate from the full schema at `/docs`.

Integrators (scanners, CI/CD pipelines) see just the endpoints they may call, with the contract
that matters to them: authentication, key permissions, errors and rate limits. The internal
endpoints the web UI uses are not part of that contract and aren't listed.
"""

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse

from app.api import v1

PREFIX = "/api/v1"
DOCS_URL = f"{PREFIX}/docs"
OPENAPI_URL = f"{PREFIX}/openapi.json"

DESCRIPTION = """
Create Jira tickets for Non-Human Identity findings from scanners and CI/CD pipelines.

## Authentication

Create an API key in IdentityHub under **Settings → API keys** and send it on every request:

```
Authorization: Bearer ihub_...
```

A key acts as the IdentityHub user who created it, through that user's Jira connection, so Jira's
own permissions always apply. Each key is also limited to the projects chosen when it was created,
and expires. Its permissions can't be changed later; create a new key instead.

## Errors

Errors are JSON with a readable `detail` and a stable `code` to branch on:

```json
{"detail": "This API key isn't allowed to create tickets in OPS. It is limited to: SEC.", "code": "api_key_project_forbidden"}
```

Request validation errors (422) list each invalid field under `detail`.

## Rate limits

60 requests per minute per key, and 120 per minute per client IP. A `429` response carries a
`Retry-After` header (seconds). Jira's own limits are passed on the same way.
"""


def install(app: FastAPI) -> None:
    """Serve the public API's schema and Swagger UI under /api/v1."""

    def public_schema() -> dict[str, Any]:
        if not hasattr(app.state, "public_openapi"):
            app.state.public_openapi = get_openapi(
                title="IdentityHub public API",
                version="1",
                description=DESCRIPTION,
                routes=v1.router.routes,  # paths relative to /api, hence `servers`
                servers=[{"url": "/api"}],
            )
        return app.state.public_openapi

    @app.get(OPENAPI_URL, include_in_schema=False)
    async def openapi() -> dict[str, Any]:
        return public_schema()

    @app.get(DOCS_URL, include_in_schema=False)
    async def docs() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url=OPENAPI_URL, title="IdentityHub public API")
