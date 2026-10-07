"""API keys (management + permissions) and the public REST API, against a mocked Jira."""

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update

from app.db.models import ApiKey, Finding
from helpers import JIRA_API, connect, csrf, sign_up_and_login

PERMITTED_PROJECTS = {"values": [{"id": "1", "key": "SEC", "name": "Security"}, {"id": "2", "key": "PLAT", "name": "Platform"}]}
FINDING = {"project_key": "SEC", "summary": "Exposed key in CI logs: ci-runner-key", "severity": "critical"}


@pytest.fixture
async def connected(client, atlassian):
    await sign_up_and_login(client, "alice@example.com")
    await connect(client)
    atlassian.jira.get(f"{JIRA_API}/project/search", json=PERMITTED_PROJECTS)
    atlassian.jira.get(f"{JIRA_API}/issue/createmeta/SEC/issuetypes", json={"issueTypes": [{"id": "3", "name": "Task"}]})
    atlassian.jira.post(f"{JIRA_API}/issue", json={"id": "10001", "key": "SEC-7"}, status=201)
    return client


async def create_key(client, projects=("sec",), scopes=("findings:create",), **overrides):
    body = {
        "name": "CI scanner",
        "expires_in_days": 30,
        "permissions": {"scopes": list(scopes), "projects": list(projects)},
        **overrides,
    }
    return await client.post("/api/api-keys", json=body, headers=await csrf(client))


@pytest.fixture
async def api_key(connected) -> str:
    response = await create_key(connected)
    assert response.status_code == 201
    return response.json()["key"]


@pytest.fixture
async def anonymous(app):
    """A client with no cookies at all, like a CI job."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


def post_finding(client, key: str | None, body=FINDING):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return client.post("/api/v1/findings", json=body, headers=headers)


# --- Managing keys ----------------------------------------------------------------------------


async def test_create_key_shows_plaintext_once_and_stores_only_a_hash(connected, atlassian, db):
    response = await create_key(connected, projects=["sec", "PLAT", "SEC"], notes="Runs nightly in infra-ci")
    assert response.status_code == 201
    created = response.json()
    assert created["key"].startswith("ihub_") and len(created["key"]) > 40
    assert created["prefix"] == created["key"][:9]  # "ihub_" + 4 characters
    # Normalised and de-duplicated, stored as a versioned document.
    assert created["permissions"] == {"version": 1, "scopes": ["findings:create"], "projects": ["SEC", "PLAT"]}
    assert created["notes"] == "Runs nightly in infra-ci"
    assert created["status"] == "active"
    expires = datetime.fromisoformat(created["expires_at"])
    assert timedelta(days=29) < expires - datetime.now(UTC) <= timedelta(days=30)

    # Projects were checked against what the user can create issues in.
    params = parse_qs(urlparse(atlassian.jira.calls[-1].request.url).query)
    assert params["action"] == ["create"] and params["keys"] == ["SEC", "PLAT"]

    raw = (await db.execute(text("SELECT key_hash, prefix FROM api_key"))).one()
    assert created["key"] not in raw.key_hash and len(raw.key_hash) == 64

    listed = (await connected.get("/api/api-keys")).json()
    assert [k["name"] for k in listed] == ["CI scanner"]
    assert "key" not in listed[0]


async def test_cannot_scope_a_key_to_projects_you_cannot_create_in(connected):
    response = await create_key(connected, projects=["SEC", "HR"])
    assert response.status_code == 400
    assert response.json() == {
        "detail": "Your Jira account can't create issues in HR, so an API key can't be allowed to either. "
        "Choose projects from the list.",
        "code": "api_key_projects_not_permitted",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"projects": []},
        {"scopes": []},
        {"scopes": ["admin:everything"]},  # only known scopes can be granted
        {"expires_in_days": 0},
        {"expires_in_days": 3650},  # no near-permanent keys
        {"name": ""},
        {"notes": "x" * 1001},
        {"projects": ["not a key"]},
        {"is_admin": True},  # unknown fields are rejected
    ],
)
async def test_invalid_key_requests(connected, overrides):
    assert (await create_key(connected, **overrides)).status_code == 422


async def test_creating_keys_requires_a_jira_connection(client):
    await sign_up_and_login(client, "alice@example.com")
    response = await create_key(client)
    assert response.status_code == 409 and response.json()["code"] == "jira_not_connected"


async def test_revoke(connected, api_key, anonymous):
    key_id = (await connected.get("/api/api-keys")).json()[0]["id"]
    assert (await connected.delete(f"/api/api-keys/{key_id}", headers=await csrf(connected))).status_code == 204
    assert (await connected.get("/api/api-keys")).json()[0]["status"] == "revoked"

    response = await post_finding(anonymous, api_key)
    assert response.status_code == 401 and response.json()["code"] == "api_key_revoked"


async def test_users_cannot_see_or_revoke_each_others_keys(connected, api_key, app):
    alice_key_id = (await connected.get("/api/api-keys")).json()[0]["id"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bob:
        await sign_up_and_login(bob, "bob@example.com")
        assert (await bob.get("/api/api-keys")).json() == []
        response = await bob.delete(f"/api/api-keys/{alice_key_id}", headers=await csrf(bob))
        assert response.status_code == 404  # indistinguishable from a key that doesn't exist
    assert (await connected.get("/api/api-keys")).json()[0]["status"] == "active"


async def test_active_key_limit(connected, db, monkeypatch):
    from app.services import api_keys

    monkeypatch.setattr(api_keys, "MAX_ACTIVE_KEYS", 2)
    assert (await create_key(connected)).status_code == 201
    assert (await create_key(connected)).status_code == 201
    response = await create_key(connected)
    assert response.status_code == 409 and response.json()["code"] == "api_key_limit_reached"


# --- Public REST API --------------------------------------------------------------------------


async def test_create_finding_with_api_key(api_key, anonymous, atlassian, db):
    response = await post_finding(anonymous, api_key)
    assert response.status_code == 201
    assert response.headers["location"] == "https://acme.atlassian.net/browse/SEC-7"
    assert response.json() == {
        "key": "SEC-7",
        "url": "https://acme.atlassian.net/browse/SEC-7",
        "summary": "Exposed key in CI logs: ci-runner-key",
    }
    fields = json.loads(atlassian.jira.calls[-1].request.body)["fields"]
    assert fields["labels"] == ["identityhub", "severity-critical"]

    finding = await db.scalar(select(Finding))
    key = await db.scalar(select(ApiKey))
    assert finding.source.value == "api" and finding.api_key_id == key.id
    assert key.last_used_at is not None


async def test_missing_key(anonymous):
    response = await post_finding(anonymous, None)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "detail": "An API key is required. Send it as 'Authorization: Bearer <key>'.",
        "code": "api_key_missing",
    }


@pytest.mark.parametrize("key", ["ihub_not-a-real-key", "some-other-token", "ihub_"])
async def test_invalid_key(api_key, anonymous, key):
    response = await post_finding(anonymous, key)
    assert response.status_code == 401 and response.json()["code"] == "api_key_invalid"


async def test_expired_key(api_key, anonymous, db):
    await db.execute(update(ApiKey).values(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
    await db.commit()
    response = await post_finding(anonymous, api_key)
    assert response.status_code == 401
    assert response.json()["code"] == "api_key_expired"
    assert "expired on" in response.json()["detail"]


async def test_key_cannot_post_outside_its_projects(api_key, anonymous, atlassian):
    calls_before = len(atlassian.jira.calls)
    response = await post_finding(anonymous, api_key, {**FINDING, "project_key": "PLAT"})
    assert response.status_code == 403
    assert response.json() == {
        "detail": "This API key isn't allowed to create tickets in PLAT. It is limited to: SEC.",
        "code": "api_key_project_forbidden",
    }
    assert len(atlassian.jira.calls) == calls_before  # refused before touching Jira


async def test_session_cookie_is_not_accepted(connected):
    # A logged-in browser can't use the public API with its cookie (and so can't be CSRF'd into it).
    response = await post_finding(connected, None)
    assert response.status_code == 401 and response.json()["code"] == "api_key_missing"


async def test_input_is_validated(api_key, anonymous):
    response = await post_finding(anonymous, api_key, {"project_key": "SEC"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "summary"]


async def test_owner_disconnected_jira(connected, api_key, anonymous):
    await connected.delete("/api/jira/connection", headers=await csrf(connected))
    response = await post_finding(anonymous, api_key)
    assert response.status_code == 409 and response.json()["code"] == "jira_not_connected"


async def test_deactivated_owner(api_key, anonymous, db):
    from app.db.models import User

    await db.execute(update(User).values(is_active=False))
    await db.commit()
    assert (await post_finding(anonymous, api_key)).status_code == 401


async def test_per_key_rate_limit(api_key, anonymous, monkeypatch):
    from limits import parse

    from app.api import v1

    monkeypatch.setattr(v1, "PER_KEY_LIMIT", parse("2/minute"))
    assert (await post_finding(anonymous, api_key)).status_code == 201
    assert (await post_finding(anonymous, api_key)).status_code == 201
    response = await post_finding(anonymous, api_key)
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0
    assert response.json()["code"] == "rate_limited"


# --- Notes are editable; permissions are not --------------------------------------------------


async def test_notes_can_be_edited(connected, api_key):
    key_id = (await connected.get("/api/api-keys")).json()[0]["id"]
    response = await connected.patch(
        f"/api/api-keys/{key_id}", json={"notes": "Owned by the platform team"}, headers=await csrf(connected)
    )
    assert response.status_code == 200 and response.json()["notes"] == "Owned by the platform team"


@pytest.mark.parametrize(
    "change",
    [
        {"permissions": {"scopes": ["findings:create"], "projects": ["SEC", "PLAT"]}},
        {"expires_in_days": 365},
        {"name": "renamed"},
    ],
)
async def test_permissions_cannot_be_changed_after_creation(connected, api_key, db, change):
    key_id = (await connected.get("/api/api-keys")).json()[0]["id"]
    before = (await db.execute(text("SELECT permissions, expires_at, name FROM api_key"))).one()
    response = await connected.patch(f"/api/api-keys/{key_id}", json={"notes": "x", **change}, headers=await csrf(connected))
    assert response.status_code == 422
    assert (await db.execute(text("SELECT permissions, expires_at, name FROM api_key"))).one() == before


async def test_key_with_no_scopes_is_refused(api_key, anonymous, db):
    await db.execute(update(ApiKey).values(permissions={"version": 1, "scopes": [], "projects": ["SEC"]}))
    await db.commit()
    response = await post_finding(anonymous, api_key)
    # An empty scope list is itself invalid, so the key is refused as unreadable: fail closed.
    assert response.status_code == 401 and response.json()["code"] == "api_key_permissions_unreadable"


@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings")  # listing the unreadable key, on purpose
async def test_unknown_permission_fields_fail_closed(api_key, anonymous, db, connected):
    # e.g. a newer release added {"allowed_ips": [...]}, then the deployment was rolled back:
    # this version must not silently ignore a restriction it doesn't understand.
    await db.execute(
        update(ApiKey).values(
            permissions={"version": 1, "scopes": ["findings:create"], "projects": ["SEC"], "allowed_ips": ["10.0.0.0/8"]}
        )
    )
    await db.commit()
    response = await post_finding(anonymous, api_key)
    assert response.status_code == 401 and response.json()["code"] == "api_key_permissions_unreadable"
    # ...but the key is still listed, so the user can see and revoke it.
    assert (await connected.get("/api/api-keys")).status_code == 200


async def test_public_api_reference_lists_only_the_public_api(client):
    schema = (await client.get("/api/v1/openapi.json")).json()
    assert schema["info"]["title"] == "IdentityHub public API"
    assert schema["servers"] == [{"url": "/api"}] and list(schema["paths"]) == ["/v1/findings"]
    assert "ApiKey" in schema["components"]["securitySchemes"]
    assert "ErrorBody" in schema["components"]["schemas"]
    page = await client.get("/api/v1/docs")
    assert page.status_code == 200 and "/api/v1/openapi.json" in page.text


async def test_full_schema_is_only_served_in_development(client, monkeypatch):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 404, path

    from app.core.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("ENVIRONMENT", "development")
    get_settings.cache_clear()
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as dev:
        assert (await dev.get("/docs")).status_code == 200
        assert "/api/findings" in (await dev.get("/openapi.json")).json()["paths"]
