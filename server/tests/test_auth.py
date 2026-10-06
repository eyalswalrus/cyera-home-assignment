from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import update

from helpers import PASSWORD, csrf, login, register


async def test_register_login_me_logout(client):
    assert (await register(client, "alice@example.com")).status_code == 201

    response = await login(client, "alice@example.com")
    assert response.status_code == 204
    set_cookie = response.headers["set-cookie"].lower()
    assert "identityhub_session=" in set_cookie and "httponly" in set_cookie and "samesite=lax" in set_cookie

    me = await client.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["email"] == "alice@example.com"

    assert (await client.post("/api/auth/logout", headers=await csrf(client))).status_code == 204
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_logout_revokes_session_server_side(client):
    await register(client, "alice@example.com")
    await login(client, "alice@example.com")
    stolen = client.cookies["identityhub_session"]

    await client.post("/api/auth/logout", headers=await csrf(client))

    # Replaying the old cookie must fail: the session row is gone, not just the browser cookie.
    client.cookies.set("identityhub_session", stolen)
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_session_expires_after_lifetime(client, db):
    from app.auth.users import SESSION_LIFETIME_SECONDS
    from app.db.models import AccessToken

    await register(client, "alice@example.com")
    await login(client, "alice@example.com")
    assert (await client.get("/api/auth/me")).status_code == 200

    expired = datetime.now(UTC) - timedelta(seconds=SESSION_LIFETIME_SECONDS + 60)
    await db.execute(update(AccessToken).values(created_at=expired))
    await db.commit()

    assert (await client.get("/api/auth/me")).status_code == 401


async def test_users_only_see_themselves(client, app):
    from httpx import ASGITransport

    await register(client, "alice@example.com")
    await login(client, "alice@example.com")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bob:
        await register(bob, "bob@example.com")
        await login(bob, "bob@example.com")
        assert (await bob.get("/api/auth/me")).json()["email"] == "bob@example.com"

    assert (await client.get("/api/auth/me")).json()["email"] == "alice@example.com"


async def test_wrong_password_is_rejected(client):
    await register(client, "alice@example.com")
    response = await login(client, "alice@example.com", "not-the-password")
    assert response.status_code == 400
    assert response.json()["detail"] == "LOGIN_BAD_CREDENTIALS"


async def test_weak_password_has_clear_reason(client):
    response = await register(client, "alice@example.com", "short")
    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "Password must be at least 12 characters long."

    response = await register(client, "alice@example.com", "alice-is-my-password")
    assert response.json()["detail"]["reason"] == "Password must not contain your email address."


async def test_duplicate_email_is_rejected(client):
    await register(client, "alice@example.com")
    response = await register(client, "Alice@Example.com")
    assert response.status_code == 400
    assert response.json()["detail"] == "REGISTER_USER_ALREADY_EXISTS"


async def test_register_cannot_escalate_privileges(client, db):
    from sqlalchemy import select

    from app.db.models import User

    await client.post(
        "/api/auth/register",
        json={"email": "eve@example.com", "password": PASSWORD, "is_superuser": True, "is_verified": True},
        headers=await csrf(client),
    )
    user = await db.scalar(select(User).where(User.email == "eve@example.com"))
    assert user is not None and not user.is_superuser and not user.is_verified


async def test_state_changing_requests_require_csrf_token(client):
    response = await client.post("/api/auth/register", json={"email": "a@example.com", "password": PASSWORD})
    assert response.status_code == 403
    assert "security token" in response.json()["detail"]

    # A token that doesn't match the cookie is rejected too.
    await client.get("/api/health")
    response = await client.post(
        "/api/auth/register",
        json={"email": "a@example.com", "password": PASSWORD},
        headers={"X-CSRFToken": "forged"},
    )
    assert response.status_code == 403


async def test_public_api_is_exempt_from_csrf(client):
    # /api/v1 authenticates with API keys, not cookies: without a CSRF token the request still
    # reaches the endpoint, which then asks for an API key (401, not a CSRF 403).
    response = await client.post("/api/v1/findings", json={})
    assert response.status_code == 401 and response.json()["code"] == "api_key_missing"


async def test_login_is_rate_limited(client):
    await register(client, "alice@example.com")
    for _ in range(9):  # register used 1 of the 10/minute budget
        await login(client, "alice@example.com", "wrong-password-123")

    response = await login(client, "alice@example.com")
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0
    assert "Too many attempts" in response.json()["detail"]


def _csp(header: str) -> dict[str, str]:
    return dict(d.strip().split(" ", 1) for d in header.split(";") if " " in d.strip())


async def test_security_headers(client):
    headers = (await client.get("/api/health")).headers
    csp = _csp(headers["content-security-policy"])
    assert csp["script-src"] == "'self'"  # no inline or third-party scripts in the app
    assert csp["frame-ancestors"] == "'none'"
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert "strict-transport-security" not in headers  # plain HTTP in tests

    docs_csp = _csp((await client.get("/docs")).headers["content-security-policy"])
    assert "https://cdn.jsdelivr.net" in docs_csp["script-src"]
