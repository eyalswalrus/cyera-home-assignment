"""Atlassian OAuth 2.0 (3LO): authorization, token refresh and site discovery (via Authlib).

Reference: https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/
"""

from dataclasses import dataclass
from typing import Any

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.integrations.starlette_client import OAuth, OAuthError

from app.core.config import Settings
from app.jira.errors import JiraReauthRequired, JiraUnavailable

AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
API_BASE_URL = "https://api.atlassian.com/ex/jira/{cloud_id}"

# Least privilege: read/write issues and projects, read the user's own profile, and
# `offline_access` for a refresh token so the user doesn't have to reconnect every hour.
SCOPES = "read:jira-work write:jira-work read:jira-user offline_access"
# Atlassian expects client credentials in the token request body.
TOKEN_AUTH_METHOD = "client_secret_post"


@dataclass(frozen=True)
class JiraSite:
    cloud_id: str
    url: str
    name: str


def build_oauth(settings: Settings) -> OAuth:
    oauth = OAuth()
    oauth.register(
        name="atlassian",
        client_id=settings.atlassian_client_id,
        client_secret=settings.atlassian_client_secret.get_secret_value() if settings.atlassian_client_secret else None,
        authorize_url=AUTHORIZE_URL,
        access_token_url=TOKEN_URL,
        client_kwargs={"scope": SCOPES, "token_endpoint_auth_method": TOKEN_AUTH_METHOD},
        # `audience` is required by Atlassian; `prompt=consent` makes the consent screen (and the
        # scopes being granted) explicit every time the user connects.
        authorize_params={"audience": "api.atlassian.com", "prompt": "consent"},
    )
    return oauth


def callback_url(settings: Settings) -> str:
    # Must exactly match the callback URL registered in the Atlassian developer console.
    return f"{settings.app_base_url.rstrip('/')}/api/jira/callback"


async def refresh_token(settings: Settings, token: dict[str, Any]) -> dict[str, Any]:
    """Exchange the refresh token for a new token set.

    Atlassian rotates refresh tokens: the response contains a new refresh token and the old one
    stops working, so the caller must persist the returned token before anything else uses it.
    """
    secret = settings.atlassian_client_secret.get_secret_value() if settings.atlassian_client_secret else None
    async with AsyncOAuth2Client(
        client_id=settings.atlassian_client_id,
        client_secret=secret,
        token_endpoint_auth_method=TOKEN_AUTH_METHOD,
    ) as client:
        try:
            new_token = await client.refresh_token(TOKEN_URL, refresh_token=token.get("refresh_token"))
        except OAuthError as exc:
            # invalid_grant: the refresh token expired, was revoked, or was already rotated away.
            if exc.error in {"invalid_grant", "unauthorized_client", "access_denied"}:
                raise JiraReauthRequired() from exc
            raise JiraUnavailable() from exc
        except httpx.HTTPError as exc:
            raise JiraUnavailable() from exc
    return dict(new_token)


async def fetch_sites(access_token: str) -> list[JiraSite]:
    """Jira sites the token can act on. The endpoint also lists e.g. Confluence sites, so keep
    only resources where we were granted the Jira write scope."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                ACCESSIBLE_RESOURCES_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
    except httpx.HTTPError as exc:
        raise JiraUnavailable() from exc
    if response.status_code == 401:
        raise JiraReauthRequired()
    if response.status_code != 200:
        raise JiraUnavailable()
    return [
        JiraSite(cloud_id=r["id"], url=r["url"], name=r["name"])
        for r in response.json()
        if "write:jira-work" in r.get("scopes", [])
    ]
