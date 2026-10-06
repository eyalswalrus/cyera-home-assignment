import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, text

from app.core import crypto
from app.core.config import get_settings
from app.db.models import JiraConnection, User

TOKEN = {"access_token": "super-secret-access", "refresh_token": "super-secret-refresh"}


async def _add_connection(db, email: str = "a@example.com") -> JiraConnection:
    user = User(email=email, hashed_password="x")
    db.add(user)
    await db.flush()
    connection = JiraConnection(
        user_id=user.id, cloud_id="c1", site_url="https://x.atlassian.net", site_name="x", token=TOKEN
    )
    db.add(connection)
    await db.commit()
    return connection


def _use_keys(monkeypatch, keys: str) -> None:
    monkeypatch.setenv("ENCRYPTION_KEYS", keys)
    get_settings.cache_clear()
    crypto.get_fernet.cache_clear()


async def test_health(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "jira_configured": True}


async def test_tokens_are_encrypted_at_rest(db):
    connection = await _add_connection(db)

    raw = (await db.execute(text("SELECT token FROM jira_connection"))).scalar_one()
    assert "super-secret" not in raw

    loaded = await db.get(JiraConnection, connection.id, populate_existing=True)
    assert loaded is not None and loaded.token == TOKEN


async def test_enums_are_stored_by_value(db):
    await _add_connection(db)
    assert (await db.execute(text("SELECT status FROM jira_connection"))).scalar_one() == "active"


async def test_deleting_user_cascades_to_their_data(db):
    await _add_connection(db, "a@example.com")
    await _add_connection(db, "b@example.com")

    await db.execute(delete(User).where(User.email == "a@example.com"))
    await db.commit()

    assert await db.scalar(select(func.count()).select_from(JiraConnection)) == 1


async def test_key_rotation_allows_removing_old_key(db, monkeypatch):
    from app.db.rotate_keys import rotate

    old_key, new_key = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    _use_keys(monkeypatch, old_key)
    connection = await _add_connection(db)

    _use_keys(monkeypatch, f"{new_key},{old_key}")
    assert await rotate() == 1

    _use_keys(monkeypatch, new_key)  # old key retired
    loaded = await db.get(JiraConnection, connection.id, populate_existing=True)
    assert loaded is not None and loaded.token == TOKEN


def test_invalid_config_fails_with_actionable_message(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEYS", "not-a-fernet-key")
    monkeypatch.delenv("SECRET_KEY")
    get_settings.cache_clear()
    with pytest.raises(SystemExit) as exc:
        get_settings()
    message = str(exc.value)
    assert "SECRET_KEY" in message and "ENCRYPTION_KEYS" in message and "init_env.py" in message


async def test_spa_fallback_only_for_page_routes(tmp_path, monkeypatch):
    from app.main import create_app

    (tmp_path / "index.html").write_text("<title>IdentityHub</title>")
    monkeypatch.setenv("STATIC_DIR", str(tmp_path))
    get_settings.cache_clear()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert "IdentityHub" in (await ac.get("/settings")).text
        assert (await ac.get("/api/does-not-exist")).status_code == 404
        assert (await ac.get("/assets/missing.js")).status_code == 404
