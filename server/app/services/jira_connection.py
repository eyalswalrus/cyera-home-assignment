"""A user's Jira connection: creating it from an OAuth grant, choosing a site, keeping the access
token fresh, and disconnecting. Every function is scoped to one user."""

import asyncio
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import JiraConnection, JiraConnectionStatus, User
from app.jira import oauth
from app.jira.client import ActiveConnection, get_account
from app.jira.errors import (
    JiraNoSites,
    JiraNotConfigured,
    JiraNotConnected,
    JiraReauthRequired,
    JiraSiteNotFound,
    JiraSiteSelectionRequired,
)
from app.jira.oauth import JiraSite

# Refresh slightly before expiry so a token can't expire mid-request.
REFRESH_MARGIN_SECONDS = 60

# One lock per connection. Atlassian rotates refresh tokens: if two requests refreshed at once,
# the second would present a refresh token the first had just invalidated, and the user would be
# logged out of Jira. In-process only - several workers would need a distributed lock.
_refresh_locks: defaultdict[uuid.UUID, asyncio.Lock] = defaultdict(asyncio.Lock)


def require_configured() -> None:
    if not get_settings().jira_configured:
        raise JiraNotConfigured()


async def get_connection(db: AsyncSession, user: User) -> JiraConnection | None:
    return await db.scalar(select(JiraConnection).where(JiraConnection.user_id == user.id))


async def complete_authorization(db: AsyncSession, user: User, token: dict[str, Any]) -> JiraConnection:
    """Store the token from a finished OAuth flow. Reconnecting replaces any previous connection."""
    sites = await oauth.fetch_sites(token["access_token"])
    if not sites:
        raise JiraNoSites()

    connection = await get_connection(db, user) or JiraConnection(user_id=user.id)
    connection.token = token
    connection.cloud_id = connection.site_url = connection.site_name = None
    connection.account_id = connection.account_name = None
    if len(sites) == 1:
        await _attach_site(connection, sites[0], token["access_token"])
    else:
        connection.status = JiraConnectionStatus.NEEDS_SITE
    db.add(connection)
    await db.commit()
    return connection


async def list_sites(db: AsyncSession, user: User) -> list[JiraSite]:
    connection = await _require_connection(db, user)
    return await oauth.fetch_sites(await _access_token(db, connection))


async def select_site(db: AsyncSession, user: User, cloud_id: str) -> JiraConnection:
    connection = await _require_connection(db, user)
    access_token = await _access_token(db, connection)
    site = next((s for s in await oauth.fetch_sites(access_token) if s.cloud_id == cloud_id), None)
    if site is None:
        raise JiraSiteNotFound()
    await _attach_site(connection, site, access_token)
    await db.commit()
    return connection


async def disconnect(db: AsyncSession, user: User) -> None:
    # Deletes our copy of the tokens. Atlassian has no token-revocation endpoint for 3LO apps; the
    # user can also remove the app under "Connected apps" in their Atlassian account settings.
    await db.execute(delete(JiraConnection).where(JiraConnection.user_id == user.id))
    await db.commit()


async def get_active_connection(db: AsyncSession, user: User) -> ActiveConnection:
    """The user's usable connection with a fresh access token, or a JiraError explaining what the
    user must do first (connect, choose a site, reconnect)."""
    connection = await _require_connection(db, user)
    if connection.status == JiraConnectionStatus.NEEDS_SITE:
        raise JiraSiteSelectionRequired()
    access_token = await _access_token(db, connection)
    assert connection.cloud_id is not None and connection.site_url is not None
    return ActiveConnection(connection.cloud_id, connection.site_url, access_token)


@asynccontextmanager
async def use_jira(db: AsyncSession, user: User) -> AsyncIterator[ActiveConnection]:
    """Call Jira as `user`. If Jira rejects the token anyway (e.g. the user revoked the app in their
    Atlassian account), the connection is marked as needing reauthorization so the UI says so."""
    active = await get_active_connection(db, user)
    try:
        yield active
    except JiraReauthRequired:
        await db.execute(
            update(JiraConnection)
            .where(JiraConnection.user_id == user.id)
            .values(status=JiraConnectionStatus.NEEDS_REAUTH)
        )
        await db.commit()
        raise


async def _require_connection(db: AsyncSession, user: User) -> JiraConnection:
    require_configured()
    connection = await get_connection(db, user)
    if connection is None:
        raise JiraNotConnected()
    if connection.status == JiraConnectionStatus.NEEDS_REAUTH:
        raise JiraReauthRequired()
    return connection


async def _attach_site(connection: JiraConnection, site: JiraSite, access_token: str) -> None:
    myself = await get_account(ActiveConnection(site.cloud_id, site.url, access_token))
    connection.cloud_id, connection.site_url, connection.site_name = site.cloud_id, site.url, site.name
    connection.account_id = myself.get("accountId")
    connection.account_name = myself.get("displayName")
    connection.status = JiraConnectionStatus.ACTIVE


def _is_expiring(token: dict[str, Any]) -> bool:
    return float(token.get("expires_at", 0)) - time.time() < REFRESH_MARGIN_SECONDS


async def _access_token(db: AsyncSession, connection: JiraConnection) -> str:
    if connection.token is not None and not _is_expiring(connection.token):
        return connection.token["access_token"]

    async with _refresh_locks[connection.id]:
        # Another request may have refreshed while we waited for the lock: re-read before acting.
        await db.refresh(connection)
        try:
            if connection.token is None:  # undecryptable, see EncryptedJSON
                raise JiraReauthRequired()
            if _is_expiring(connection.token):
                connection.token = await oauth.refresh_token(get_settings(), connection.token)
                await db.commit()
        except JiraReauthRequired:
            connection.status = JiraConnectionStatus.NEEDS_REAUTH
            await db.commit()
            raise
        return connection.token["access_token"]
