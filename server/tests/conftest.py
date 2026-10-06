import re
from collections.abc import AsyncIterator, Iterator

import pytest
import httpx
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from responses import RequestsMock
from sqlalchemy.ext.asyncio import AsyncSession

from app.jira.oauth import ACCESSIBLE_RESOURCES_URL, TOKEN_URL
from helpers import CONFLUENCE_ONLY, SITE_A, MockAtlassian, token_response


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
async def app() -> AsyncIterator[FastAPI]:
    from app.main import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
def atlassian() -> Iterator[MockAtlassian]:
    """Mocked Atlassian: OAuth endpoints (respx) and Jira REST (responses). Tests add or override
    routes as needed."""
    with (
        respx.mock(assert_all_called=False) as oauth,
        RequestsMock(assert_all_requests_are_fired=False) as jira,
    ):
        oauth.post(TOKEN_URL).mock(return_value=token_response())
        oauth.get(ACCESSIBLE_RESOURCES_URL).mock(return_value=httpx.Response(200, json=[SITE_A, CONFLUENCE_ONLY]))
        jira.get(
            re.compile(r"https://api\.atlassian\.com/ex/jira/[^/]+/rest/api/3/myself"),
            json={"accountId": "acc-123", "displayName": "Alice Atlassian"},
        )
        yield MockAtlassian(oauth=oauth, jira=jira)
