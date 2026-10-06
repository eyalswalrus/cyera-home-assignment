"""Public REST API for external systems (scanners, CI/CD pipelines), authenticated with API keys.

Versioned under /api/v1 so the contract can evolve without breaking existing integrations. It never
accepts the browser session cookie, which is also why it is exempt from CSRF checks.
"""

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from limits import parse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.rate_limit import RateLimit, enforce_rate_limit
from app.db.models import FindingSource
from app.db.session import get_db
from app.schemas.api_keys import Scope
from app.schemas.findings import FindingCreate, FindingCreated
from app.services import api_keys, findings
from app.services.api_keys import ApiKeyMissing, AuthenticatedKey

PER_KEY_LIMIT = parse("60/minute")

_bearer = HTTPBearer(
    auto_error=False,  # we answer with our own 401 message
    scheme_name="ApiKey",
    description="An IdentityHub API key (`ihub_...`), created under Settings → API keys.",
)


async def require_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> AuthenticatedKey:
    if credentials is None:
        raise ApiKeyMissing(headers={"WWW-Authenticate": "Bearer"})
    try:
        auth = await api_keys.authenticate(db, credentials.credentials)
    except AppError as exc:
        exc.headers = {"WWW-Authenticate": 'Bearer error="invalid_token"'}
        raise
    await enforce_rate_limit(
        request,
        PER_KEY_LIMIT,
        "api_v1_key",
        str(auth.key.id),
        "Rate limit exceeded for this API key (60 requests per minute). Retry in {seconds} seconds.",
    )
    return auth


router = APIRouter(
    prefix="/v1",
    tags=["public API v1"],
    # Per-IP ceiling in front of key checks, so unauthenticated floods are throttled too.
    dependencies=[Depends(RateLimit("api_v1_ip", "120/minute"))],
)

_ERRORS = {
    401: {"description": "Missing, invalid, revoked or expired API key"},
    403: {"description": "The key lacks the permission or project, or its owner lacks Jira permission"},
    404: {"description": "Project not found in Jira, or not visible to the key owner"},
    409: {"description": "The key owner's Jira connection needs attention (not connected / reconnect)"},
    422: {"description": "Invalid input, or Jira rejected the ticket"},
    429: {"description": "Rate limit exceeded; see the Retry-After header"},
    502: {"description": "Jira is unavailable"},
}


@router.post(
    "/findings",
    status_code=status.HTTP_201_CREATED,
    summary="Create an NHI finding ticket",
    response_description="The created Jira issue. `Location` points to it.",
    responses=_ERRORS,
)
async def create_finding(
    body: FindingCreate,
    response: Response,
    auth: AuthenticatedKey = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
) -> FindingCreated:
    """Creates a Jira issue in `project_key`, acting as the API key's owner.

    The key must be allowed to post to that project, and the owner's Jira account must be able to
    create issues there.
    """
    api_keys.require_scope(auth, Scope.FINDINGS_CREATE)
    api_keys.ensure_project_allowed(auth, body.project_key)
    created = await findings.create_finding(db, auth.user, body, FindingSource.API, api_key_id=auth.key.id)
    response.headers["Location"] = created.url
    return created
