"""Database schema.

Tenancy model: the tenant is the individual user. Every tenant-owned table carries a non-null
`user_id` foreign key, and repositories always filter on the authenticated user's id - never on
an id supplied by the client.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from fastapi_users_db_sqlalchemy import SQLAlchemyBaseUserTableUUID
from fastapi_users_db_sqlalchemy.access_token import SQLAlchemyBaseAccessTokenTableUUID
from fastapi_users_db_sqlalchemy.generics import GUID, TIMESTAMPAware, now_utc
from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.crypto import EncryptedJSON


class Base(DeclarativeBase):
    pass


def _user_fk(*, unique: bool = False) -> Mapped[uuid.UUID]:
    return mapped_column(
        GUID, ForeignKey("user.id", ondelete="cascade"), nullable=False, index=True, unique=unique
    )


def _str_enum(enum_cls: type[enum.StrEnum]) -> Enum:
    # Store the enum's value ("active"), not its Python name ("ACTIVE"), so raw rows match the API.
    return Enum(enum_cls, native_enum=False, values_callable=lambda members: [m.value for m in members])


class User(SQLAlchemyBaseUserTableUUID, Base):
    """App account (managed by fastapi-users: email, argon2 password hash, flags)."""


class AccessToken(SQLAlchemyBaseAccessTokenTableUUID, Base):
    """Server-side login session. The browser cookie holds only this opaque token;
    logging out deletes the row, so the session is revoked immediately."""


class JiraConnectionStatus(enum.StrEnum):
    ACTIVE = "active"
    # The Atlassian account can reach several Jira sites; the user must pick one.
    NEEDS_SITE = "needs_site"
    # Refresh token was revoked/expired (or can't be decrypted); the user must reconnect.
    NEEDS_REAUTH = "needs_reauth"


class JiraConnection(Base):
    """A user's OAuth (3LO) link to one Jira Cloud site."""

    __tablename__ = "jira_connection"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    # One Jira site per user for this POC.
    user_id: Mapped[uuid.UUID] = _user_fk(unique=True)
    # Site fields are empty while status is NEEDS_SITE.
    cloud_id: Mapped[str | None] = mapped_column(String(64))
    site_url: Mapped[str | None] = mapped_column(String(255))
    site_name: Mapped[str | None] = mapped_column(String(255))
    # The Atlassian identity that granted access, shown in the UI as "Connected as ...".
    account_id: Mapped[str | None] = mapped_column(String(128))
    account_name: Mapped[str | None] = mapped_column(String(255))
    # Full OAuth token set (access_token, refresh_token, expires_at, scope), encrypted at rest.
    # Loads as None if it can't be decrypted (e.g. ENCRYPTION_KEYS changed); see EncryptedJSON.
    token: Mapped[dict[str, Any] | None] = mapped_column(EncryptedJSON)
    status: Mapped[JiraConnectionStatus] = mapped_column(
        _str_enum(JiraConnectionStatus), default=JiraConnectionStatus.ACTIVE, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPAware(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPAware(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )


class ApiKey(Base):
    """Credential for the public REST API. Only a SHA-256 hash of the key is stored; the
    plaintext is shown to the user once at creation time."""

    __tablename__ = "api_key"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = _user_fk()
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Non-secret leading characters, so users can tell keys apart in the UI.
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPAware(timezone=True), default=now_utc, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(TIMESTAMPAware(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMPAware(timezone=True))


class FindingSource(enum.StrEnum):
    UI = "ui"
    API = "api"


class Finding(Base):
    """Audit record of a Jira issue created through IdentityHub."""

    __tablename__ = "finding"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = _user_fk()
    source: Mapped[FindingSource] = mapped_column(_str_enum(FindingSource), nullable=False)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(GUID, ForeignKey("api_key.id", ondelete="set null"))
    cloud_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_key: Mapped[str] = mapped_column(String(32), nullable=False)
    issue_key: Mapped[str] = mapped_column(String(64), nullable=False)
    issue_url: Mapped[str] = mapped_column(String(512), nullable=False)
    summary: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPAware(timezone=True), default=now_utc, nullable=False)
