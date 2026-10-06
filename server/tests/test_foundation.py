import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from app.core.config import get_settings


async def test_health(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "jira_configured": True}


async def test_tokens_are_encrypted_at_rest(client):
    from app.db.models import JiraConnection, User
    from app.db.session import get_db

    token = {"access_token": "super-secret-access", "refresh_token": "super-secret-refresh"}
    async for db in get_db():
        user = User(email="a@example.com", hashed_password="x")
        db.add(user)
        await db.flush()
        connection = JiraConnection(
            user_id=user.id, cloud_id="c1", site_url="https://x.atlassian.net", site_name="x", token=token
        )
        db.add(connection)
        await db.commit()

        raw = (await db.execute(text("SELECT token FROM jira_connection"))).scalar_one()
        assert "super-secret" not in raw

        loaded = await db.get(JiraConnection, connection.id, populate_existing=True)
        assert loaded is not None and loaded.token == token


def test_encryption_key_rotation(monkeypatch):
    from app.core import crypto
    from app.core.crypto import EncryptedJSON

    old_key, new_key = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    column = EncryptedJSON()

    monkeypatch.setenv("ENCRYPTION_KEYS", old_key)
    get_settings.cache_clear(); crypto.get_fernet.cache_clear()
    ciphertext = column.process_bind_param({"a": 1}, None)

    # Prepend a new key: data written with the old key must still decrypt.
    monkeypatch.setenv("ENCRYPTION_KEYS", f"{new_key},{old_key}")
    get_settings.cache_clear(); crypto.get_fernet.cache_clear()
    assert column.process_result_value(ciphertext, None) == {"a": 1}


def test_invalid_config_fails_with_actionable_message(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEYS", "not-a-fernet-key")
    monkeypatch.delenv("SECRET_KEY")
    get_settings.cache_clear()
    with pytest.raises(SystemExit) as exc:
        get_settings()
    message = str(exc.value)
    assert "SECRET_KEY" in message and "ENCRYPTION_KEYS" in message and "init_env.py" in message


async def test_spa_fallback_only_for_page_routes(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    (tmp_path / "index.html").write_text("<title>IdentityHub</title>")
    monkeypatch.setenv("STATIC_DIR", str(tmp_path))
    get_settings.cache_clear()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert "IdentityHub" in (await ac.get("/settings")).text
        assert (await ac.get("/api/does-not-exist")).status_code == 404
        assert (await ac.get("/assets/missing.js")).status_code == 404
