"""Jira OAuth connection flow against a mocked Atlassian.

`respx` mocks the httpx calls (token endpoint, accessible-resources) and `responses` mocks the
requests-based Jira REST calls made through atlassian-python-api.
"""

import asyncio
import time
from urllib.parse import parse_qs

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.db.models import JiraConnection
from app.jira.oauth import ACCESSIBLE_RESOURCES_URL, TOKEN_URL
from helpers import (
    CONFLUENCE_ONLY,
    SITE_A,
    SITE_B,
    complete_connect,
    connect,
    csrf,
    sign_up_and_login,
    start_connect,
    token_response,
)

async def expire_token(db) -> None:
    connection = await db.scalar(select(JiraConnection))
    connection.token = {**connection.token, "expires_at": time.time() - 10}
    await db.commit()


# --- Happy path -----------------------------------------------------------------------------


async def test_connect_flow(client, atlassian, db):
    await sign_up_and_login(client, "alice@example.com")

    params = await start_connect(client)
    assert params["client_id"] == ["test-client-id"]
    assert params["audience"] == ["api.atlassian.com"]
    assert params["redirect_uri"] == ["http://localhost:8000/api/jira/callback"]
    assert "offline_access" in params["scope"][0].split()
    assert params["response_type"] == ["code"]

    location = await complete_connect(client, params["state"][0])
    assert location == "http://localhost:8000/settings?jira=connected"

    # The code exchange sent the code, our callback URL and the client credentials.
    token_call = next(c for c in atlassian.oauth.calls if str(c.request.url) == TOKEN_URL)
    body = parse_qs(token_call.request.content.decode())
    assert body["grant_type"] == ["authorization_code"] and body["code"] == ["auth-code"]
    assert body["client_secret"] == ["test-client-secret"]

    status = (await client.get("/api/jira/connection")).json()
    assert status == {
        "configured": True,
        "status": "active",
        "site": {"cloud_id": "cloud-a", "name": "acme", "url": "https://acme.atlassian.net"},
        "account_name": "Alice Atlassian",
    }

    # Tokens are encrypted at rest and never returned to the browser.
    raw = (await db.execute(text("SELECT token FROM jira_connection"))).scalar_one()
    assert "access-1" not in raw and "refresh-1" not in raw
    assert "access-1" not in str(status)


async def test_users_only_see_their_own_connection(client, app, atlassian):
    await sign_up_and_login(client, "alice@example.com")
    await connect(client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bob:
        await sign_up_and_login(bob, "bob@example.com")
        assert (await bob.get("/api/jira/connection")).json()["status"] == "not_connected"
        assert (await bob.get("/api/jira/sites")).json()["code"] == "jira_not_connected"
        # Bob disconnecting must not touch Alice's connection.
        await bob.delete("/api/jira/connection", headers=await csrf(bob))

    assert (await client.get("/api/jira/connection")).json()["status"] == "active"


async def test_disconnect(client, atlassian, db):
    await sign_up_and_login(client, "alice@example.com")
    await connect(client)

    assert (await client.delete("/api/jira/connection", headers=await csrf(client))).status_code == 204
    assert (await client.get("/api/jira/connection")).json()["status"] == "not_connected"
    assert await db.scalar(select(JiraConnection)) is None


# --- Several Jira sites -----------------------------------------------------------------------


async def test_multiple_sites_require_a_choice(client, atlassian):
    atlassian.oauth.get(ACCESSIBLE_RESOURCES_URL).mock(return_value=httpx.Response(200, json=[SITE_A, SITE_B]))
    await sign_up_and_login(client, "alice@example.com")

    assert await connect(client) == "http://localhost:8000/settings?jira=choose_site"
    assert (await client.get("/api/jira/connection")).json()["status"] == "needs_site"
    assert [s["name"] for s in (await client.get("/api/jira/sites")).json()] == ["acme", "globex"]

    response = await client.put("/api/jira/connection/site", json={"cloud_id": "cloud-b"}, headers=await csrf(client))
    assert response.status_code == 200
    assert response.json()["status"] == "active" and response.json()["site"]["name"] == "globex"


async def test_cannot_select_a_site_the_account_cannot_access(client, atlassian):
    atlassian.oauth.get(ACCESSIBLE_RESOURCES_URL).mock(return_value=httpx.Response(200, json=[SITE_A, SITE_B]))
    await sign_up_and_login(client, "alice@example.com")
    await connect(client)

    response = await client.put(
        "/api/jira/connection/site", json={"cloud_id": "someone-elses-site"}, headers=await csrf(client)
    )
    assert response.status_code == 400 and response.json()["code"] == "jira_site_not_found"


async def test_account_without_jira_sites(client, atlassian):
    atlassian.oauth.get(ACCESSIBLE_RESOURCES_URL).mock(return_value=httpx.Response(200, json=[CONFLUENCE_ONLY]))
    await sign_up_and_login(client, "alice@example.com")

    assert await connect(client) == "http://localhost:8000/settings?jira_error=jira_no_sites"
    assert (await client.get("/api/jira/connection")).json()["status"] == "not_connected"


# --- Callback failures ------------------------------------------------------------------------


async def test_user_declines_consent(client, atlassian):
    await sign_up_and_login(client, "alice@example.com")
    params = await start_connect(client)
    location = await complete_connect(client, params["state"][0], error="access_denied")
    assert location == "http://localhost:8000/settings?jira_error=access_denied"


async def test_forged_state_is_rejected(client, atlassian):
    await sign_up_and_login(client, "alice@example.com")
    await start_connect(client)
    location = await complete_connect(client, "attacker-chosen-state")
    assert location == "http://localhost:8000/settings?jira_error=state_mismatch"
    assert (await client.get("/api/jira/connection")).json()["status"] == "not_connected"


async def test_rejected_code_exchange_is_not_reported_as_state_mismatch(client, atlassian):
    atlassian.oauth.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "invalid_client"}))
    await sign_up_and_login(client, "alice@example.com")
    assert await connect(client) == "http://localhost:8000/settings?jira_error=token_exchange_failed"


async def test_atlassian_unreachable_during_callback(client, atlassian):
    atlassian.oauth.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("down"))
    await sign_up_and_login(client, "alice@example.com")
    assert await connect(client) == "http://localhost:8000/settings?jira_error=jira_unavailable"


async def test_callback_for_a_different_user_is_rejected(client, atlassian):
    await sign_up_and_login(client, "alice@example.com")
    params = await start_connect(client)

    # Same browser, but a different IdentityHub user is logged in by the time Atlassian redirects.
    await client.post("/api/auth/logout", headers=await csrf(client))
    await sign_up_and_login(client, "bob@example.com")

    location = await complete_connect(client, params["state"][0])
    assert location == "http://localhost:8000/settings?jira_error=wrong_user"
    assert (await client.get("/api/jira/connection")).json()["status"] == "not_connected"


async def test_connect_requires_login(client):
    response = await client.get("/api/jira/connect")
    assert response.headers["location"] == "http://localhost:8000/settings?jira_error=not_logged_in"


async def test_not_configured(monkeypatch):
    from app.core.config import get_settings
    from app.main import create_app

    monkeypatch.delenv("ATLASSIAN_CLIENT_ID")
    monkeypatch.delenv("ATLASSIAN_CLIENT_SECRET")
    get_settings.cache_clear()
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await sign_up_and_login(client, "alice@example.com")
            assert (await client.get("/api/jira/connection")).json()["configured"] is False
            response = await client.get("/api/jira/connect")
            assert response.headers["location"].endswith("jira_error=jira_not_configured")
            response = await client.get("/api/jira/sites")
            assert response.status_code == 503 and "administrator" in response.json()["detail"]


# --- Token refresh ----------------------------------------------------------------------------


async def test_expired_token_is_refreshed_and_rotated(client, atlassian, db):
    await sign_up_and_login(client, "alice@example.com")
    await connect(client)
    await expire_token(db)
    atlassian.oauth.post(TOKEN_URL).mock(return_value=token_response("access-2", "refresh-2"))

    assert (await client.get("/api/jira/sites")).status_code == 200

    refresh_call = next(c for c in reversed(atlassian.oauth.calls) if str(c.request.url) == TOKEN_URL)
    body = parse_qs(refresh_call.request.content.decode())
    assert body["grant_type"] == ["refresh_token"] and body["refresh_token"] == ["refresh-1"]

    connection = await db.scalar(select(JiraConnection).execution_options(populate_existing=True))
    assert connection.token["access_token"] == "access-2"
    assert connection.token["refresh_token"] == "refresh-2"  # rotated token persisted


async def test_concurrent_requests_refresh_only_once(client, atlassian, db, monkeypatch):
    from app.db.models import User
    from app.db.session import get_db
    from app.jira import oauth
    from app.services import jira_connection

    await sign_up_and_login(client, "alice@example.com")
    await connect(client)
    await expire_token(db)

    calls = 0

    async def slow_refresh(settings, token):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)  # widen the race window
        return {**token, "access_token": f"access-{calls + 1}", "expires_at": time.time() + 3600}

    monkeypatch.setattr(oauth, "refresh_token", slow_refresh)

    async def use_jira() -> str:
        async for session in get_db():
            user = await session.scalar(select(User))
            return (await jira_connection.get_active_connection(session, user)).access_token

    tokens = await asyncio.gather(use_jira(), use_jira(), use_jira())
    assert calls == 1
    assert set(tokens) == {"access-2"}


async def test_revoked_refresh_token_asks_user_to_reconnect(client, atlassian, db):
    await sign_up_and_login(client, "alice@example.com")
    await connect(client)
    await expire_token(db)
    atlassian.oauth.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))

    response = await client.get("/api/jira/sites")
    assert response.status_code == 409
    assert response.json() == {
        "detail": "Your Jira connection has expired or was revoked. Reconnect Jira to continue.",
        "code": "jira_reauth_required",
    }
    assert (await client.get("/api/jira/connection")).json()["status"] == "needs_reauth"

    # Reconnecting restores the connection.
    atlassian.oauth.post(TOKEN_URL).mock(return_value=token_response("access-3", "refresh-3"))
    assert await connect(client) == "http://localhost:8000/settings?jira=connected"
    assert (await client.get("/api/jira/connection")).json()["status"] == "active"


async def test_atlassian_outage_is_reported_without_logging_user_out(client, atlassian, db):
    await sign_up_and_login(client, "alice@example.com")
    await connect(client)
    await expire_token(db)
    atlassian.oauth.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("down"))

    response = await client.get("/api/jira/sites")
    assert response.status_code == 502 and response.json()["code"] == "jira_unavailable"
    # A transient outage must not force the user to reconnect.
    assert (await client.get("/api/jira/connection")).json()["status"] == "active"


async def test_undecryptable_token_asks_user_to_reconnect(client, atlassian, monkeypatch):
    from cryptography.fernet import Fernet

    from app.core import crypto
    from app.core.config import get_settings

    await sign_up_and_login(client, "alice@example.com")
    await connect(client)

    monkeypatch.setenv("ENCRYPTION_KEYS", Fernet.generate_key().decode())  # key lost
    get_settings.cache_clear()
    crypto.get_fernet.cache_clear()

    response = await client.get("/api/jira/sites")
    assert response.status_code == 409 and response.json()["code"] == "jira_reauth_required"
