from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx
import respx
from httpx import AsyncClient
from responses import RequestsMock

PASSWORD = "correct-horse-battery"


async def csrf(client: AsyncClient) -> dict[str, str]:
    """Any GET hands out the CSRF cookie; state-changing requests must echo it in a header."""
    if "csrftoken" not in client.cookies:
        await client.get("/api/health")
    return {"X-CSRFToken": client.cookies["csrftoken"]}


async def register(client: AsyncClient, email: str, password: str = PASSWORD):
    return await client.post(
        "/api/auth/register", json={"email": email, "password": password}, headers=await csrf(client)
    )


async def login(client: AsyncClient, email: str, password: str = PASSWORD):
    return await client.post(
        "/api/auth/login", data={"username": email, "password": password}, headers=await csrf(client)
    )


async def sign_up_and_login(client: AsyncClient, email: str) -> None:
    assert (await register(client, email)).status_code == 201
    assert (await login(client, email)).status_code == 204


# --- Mocked Atlassian ---------------------------------------------------------------------------

JIRA_API = "https://api.atlassian.com/ex/jira/cloud-a/rest/api/3"


@dataclass
class MockAtlassian:
    oauth: respx.MockRouter  # httpx: token endpoint, accessible-resources
    jira: RequestsMock  # requests: Jira REST API via atlassian-python-api


SITE_A = {"id": "cloud-a", "url": "https://acme.atlassian.net", "name": "acme", "scopes": ["write:jira-work"]}
SITE_B = {"id": "cloud-b", "url": "https://globex.atlassian.net", "name": "globex", "scopes": ["write:jira-work"]}
CONFLUENCE_ONLY = {"id": "cloud-c", "url": "https://wiki.atlassian.net", "name": "wiki", "scopes": ["read:confluence"]}


def token_response(access: str = "access-1", refresh: str = "refresh-1") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "read:jira-work write:jira-work read:jira-user offline_access",
        },
    )


async def start_connect(client: AsyncClient) -> dict[str, list[str]]:
    response = await client.get("/api/jira/connect")
    assert response.status_code == 302
    location = urlparse(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == "https://auth.atlassian.com/authorize"
    return parse_qs(location.query)


async def complete_connect(client: AsyncClient, state: str, **extra: str) -> str:
    response = await client.get("/api/jira/callback", params={"code": "auth-code", "state": state, **extra})
    assert response.status_code == 302
    return response.headers["location"]


async def connect(client: AsyncClient) -> str:
    params = await start_connect(client)
    return await complete_connect(client, params["state"][0])


