"""Connecting a user's Jira account (OAuth 2.0 3LO) and managing that connection.

`/connect` and `/callback` are browser navigations, not fetch calls, so they answer with redirects
back to the UI. Failures travel as a fixed error code in the query string (never a free-text
message or a caller-supplied URL), which the UI turns into a readable message.
"""

from typing import Annotated, Literal
from urllib.parse import urlencode

import httpx
from authlib.integrations.base_client import MismatchingStateError, OAuthError
from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.users import current_active_user, fastapi_users
from app.core.config import get_settings
from app.db.models import JiraConnectionStatus, User
from app.db.session import get_db
from app.jira.errors import JiraError
from app.jira.oauth import callback_url
from app.schemas.findings import Project
from app.services import findings, jira_connection

router = APIRouter(prefix="/jira", tags=["jira"])

_optional_user = fastapi_users.current_user(active=True, optional=True)
# Ties an OAuth round-trip to the IdentityHub user who started it.
_OAUTH_USER_KEY = "jira_oauth_user_id"


def _back_to_ui(**params: str) -> RedirectResponse:
    return RedirectResponse(f"{get_settings().ui_url}/settings?{urlencode(params)}", status_code=302)


@router.get("/connect", summary="Start the Jira OAuth flow (browser redirect)")
async def connect(request: Request, user: User | None = Depends(_optional_user)) -> Response:
    if user is None:
        return _back_to_ui(jira_error="not_logged_in")
    if not get_settings().jira_configured:
        return _back_to_ui(jira_error="jira_not_configured")
    request.session[_OAUTH_USER_KEY] = str(user.id)
    return await request.app.state.oauth.atlassian.authorize_redirect(request, callback_url(get_settings()))


@router.get("/callback", summary="OAuth redirect target registered with Atlassian")
async def callback(
    request: Request,
    user: User | None = Depends(_optional_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    started_by = request.session.pop(_OAUTH_USER_KEY, None)
    if user is None:
        return _back_to_ui(jira_error="not_logged_in")
    if "error" in request.query_params:  # e.g. the user clicked "Decline" on the consent screen
        return _back_to_ui(jira_error="access_denied")
    if started_by != str(user.id):
        # Logged-in user changed mid-flow; never attach one person's Jira grant to another account.
        return _back_to_ui(jira_error="wrong_user")

    try:
        # Verifies `state` against the value stored when the flow started, then exchanges the code.
        token = await request.app.state.oauth.atlassian.authorize_access_token(request)
        connection = await jira_connection.complete_authorization(db, user, dict(token))
    except MismatchingStateError:
        # Expired (>10 min), replayed, forged, or started in another browser.
        return _back_to_ui(jira_error="state_mismatch")
    except OAuthError:
        # Atlassian refused the code exchange, e.g. wrong client credentials on our side.
        return _back_to_ui(jira_error="token_exchange_failed")
    except httpx.HTTPError:
        return _back_to_ui(jira_error="jira_unavailable")
    except JiraError as exc:
        return _back_to_ui(jira_error=exc.code)

    if connection.status == JiraConnectionStatus.NEEDS_SITE:
        return _back_to_ui(jira="choose_site")
    return _back_to_ui(jira="connected")


class SiteOut(BaseModel):
    cloud_id: str
    name: str
    url: str


class ConnectionOut(BaseModel):
    configured: bool
    status: Literal["not_connected", "active", "needs_site", "needs_reauth"]
    site: SiteOut | None = None
    account_name: str | None = None


@router.get("/connection", summary="The current user's Jira connection status")
async def get_connection(
    user: User = Depends(current_active_user), db: AsyncSession = Depends(get_db)
) -> ConnectionOut:
    configured = get_settings().jira_configured
    connection = await jira_connection.get_connection(db, user)
    if connection is None:
        return ConnectionOut(configured=configured, status="not_connected")
    site = None
    if connection.cloud_id and connection.site_name and connection.site_url:
        site = SiteOut(cloud_id=connection.cloud_id, name=connection.site_name, url=connection.site_url)
    # The token itself never leaves the server.
    return ConnectionOut(
        configured=configured, status=connection.status.value, site=site, account_name=connection.account_name
    )


@router.get("/sites", summary="Jira sites the connected Atlassian account can use")
async def list_sites(user: User = Depends(current_active_user), db: AsyncSession = Depends(get_db)) -> list[SiteOut]:
    sites = await jira_connection.list_sites(db, user)
    return [SiteOut(cloud_id=s.cloud_id, name=s.name, url=s.url) for s in sites]


class SelectSiteIn(BaseModel):
    cloud_id: str


@router.put("/connection/site", summary="Choose which Jira site to use")
async def select_site(
    body: SelectSiteIn, user: User = Depends(current_active_user), db: AsyncSession = Depends(get_db)
) -> ConnectionOut:
    await jira_connection.select_site(db, user, body.cloud_id)
    return await get_connection(user, db)


@router.delete("/connection", status_code=status.HTTP_204_NO_CONTENT, summary="Disconnect Jira")
async def disconnect(user: User = Depends(current_active_user), db: AsyncSession = Depends(get_db)) -> None:
    await jira_connection.disconnect(db, user)


@router.get("/projects", summary="Projects the user can create issues in")
async def list_projects(
    query: Annotated[str | None, Query(max_length=100, description="Filter by project name or key")] = None,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[Project]:
    return await findings.list_projects(db, user, query)
