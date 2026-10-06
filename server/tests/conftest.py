from collections.abc import AsyncIterator, Iterator

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(autouse=True)
def test_env(tmp_path, monkeypatch) -> Iterator[None]:
    """Give every test an isolated, fully-configured environment and fresh cached singletons."""
    monkeypatch.setenv("SECRET_KEY", "x" * 48)
    monkeypatch.setenv("ENCRYPTION_KEYS", Fernet.generate_key().decode())
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("ATLASSIAN_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("ATLASSIAN_CLIENT_SECRET", "test-client-secret")
    monkeypatch.delenv("STATIC_DIR", raising=False)
    # Ignore a developer's real .env so tests are hermetic.
    from app.core.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    _reset_caches()
    yield
    _reset_caches()


def _reset_caches() -> None:
    from app.core import config, crypto
    from app.db import session

    config.get_settings.cache_clear()
    crypto.get_fernet.cache_clear()
    session._engine = None
    session._sessionmaker = None


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    from app.db.session import close_db, get_db, init_db

    await init_db()
    async for session in get_db():
        yield session
    await close_db()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            yield ac
