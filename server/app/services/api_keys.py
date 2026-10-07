"""API keys for the public REST API: issuing, listing, revoking, and authenticating requests.

Keys are 256-bit random tokens. Only their SHA-256 hash is stored: a fast hash is appropriate here
(unlike passwords) because the input is already high-entropy, so it can't be brute-forced, and
lookups stay a single indexed query.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.models import ApiKey, User
from app.jira import client
from app.schemas.api_keys import (
    ALL_PROJECTS,
    MAX_PROJECTS_PER_KEY,
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    ApiKeyPermissions,
    ApiKeyStatus,
    ApiKeyUpdate,
    Scope,
)
from app.services.jira_connection import use_jira

KEY_PREFIX = "ihub_"
DISPLAY_PREFIX_LENGTH = 9  # "ihub_" + 4 random characters: enough to tell keys apart, ~24 of 256 bits
MAX_ACTIVE_KEYS = 20
# Rate-limit `last_used_at` writes: one per key per minute is precise enough for the UI.
LAST_USED_GRANULARITY = timedelta(minutes=1)

AUTH_HEADER_HINT = "Send it as 'Authorization: Bearer <key>'."


# --- Errors -----------------------------------------------------------------------------------


class ApiKeyMissing(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "api_key_missing"
    message = f"An API key is required. {AUTH_HEADER_HINT}"


class ApiKeyInvalid(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "api_key_invalid"
    message = "The API key is not valid. Check that it was copied completely."


class ApiKeyRevoked(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "api_key_revoked"
    message = "This API key has been revoked. Create a new one in IdentityHub settings."


class ApiKeyExpired(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "api_key_expired"


class ApiKeyScopeMissing(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "api_key_scope_missing"


class ApiKeyPermissionsUnreadable(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "api_key_permissions_unreadable"
    message = (
        "This API key's permissions can't be verified by this server version, so it is refused. "
        "Create a new key in IdentityHub settings."
    )


class ApiKeyProjectForbidden(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "api_key_project_forbidden"


class ApiKeyProjectsNotPermitted(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "api_key_projects_not_permitted"


class ApiKeyLimitReached(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "api_key_limit_reached"
    message = f"You can have at most {MAX_ACTIVE_KEYS} active API keys. Revoke one you no longer use."


class ApiKeyNotFound(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "api_key_not_found"
    message = "API key not found."


# --- Management (session-authenticated, scoped to the current user) ----------------------------


async def create_key(db: AsyncSession, user: User, data: ApiKeyCreate) -> ApiKeyCreated:
    active = await db.scalar(
        select(func.count()).select_from(ApiKey).where(*_active_filters(user.id, _now()))
    )
    if active is not None and active >= MAX_ACTIVE_KEYS:
        raise ApiKeyLimitReached()

    # A key can only be scoped to projects its owner can create issues in *today*. Jira still
    # enforces permissions on every request, so later permission changes are respected too. An
    # "all projects" key needs a working Jira connection but no per-project check.
    async with use_jira(db, user) as conn:
        chosen = [] if data.permissions.all_projects else list(data.permissions.projects)
        permitted = await client.search_projects(conn, None, MAX_PROJECTS_PER_KEY, keys=chosen) if chosen else []
    not_permitted = [k for k in chosen if k not in {p["key"] for p in permitted}]
    if not_permitted:
        raise ApiKeyProjectsNotPermitted(
            f"Your Jira account can't create issues in {', '.join(not_permitted)}, so an API key can't be "
            "allowed to either. Choose projects from the list."
        )

    plaintext = KEY_PREFIX + secrets.token_urlsafe(32)
    now = _now()
    api_key = ApiKey(
        user_id=user.id,
        name=data.name,
        notes=data.notes or None,
        prefix=plaintext[:DISPLAY_PREFIX_LENGTH],
        key_hash=_hash(plaintext),
        permissions=data.permissions.model_dump(mode="json"),
        created_at=now,
        expires_at=now + timedelta(days=data.expires_in_days),
    )
    db.add(api_key)
    await db.commit()
    return ApiKeyCreated(**_out(api_key, now).model_dump(), key=plaintext)


async def list_keys(db: AsyncSession, user: User) -> list[ApiKeyOut]:
    keys = await db.scalars(select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc()))
    now = _now()
    return [_out(k, now) for k in keys]


async def update_notes(db: AsyncSession, user: User, key_id: uuid.UUID, data: ApiKeyUpdate) -> ApiKeyOut:
    api_key = await _owned_key(db, user, key_id)
    api_key.notes = data.notes or None
    await db.commit()
    return _out(api_key, _now())


async def revoke_key(db: AsyncSession, user: User, key_id: uuid.UUID) -> None:
    api_key = await _owned_key(db, user, key_id)
    if api_key.revoked_at is None:
        api_key.revoked_at = _now()
        await db.commit()


# --- Authenticating REST API requests ---------------------------------------------------------


@dataclass(frozen=True)
class AuthenticatedKey:
    key: ApiKey
    user: User
    permissions: ApiKeyPermissions


async def authenticate(db: AsyncSession, presented: str) -> AuthenticatedKey:
    if not presented.startswith(KEY_PREFIX):
        raise ApiKeyInvalid()
    api_key = await db.scalar(select(ApiKey).where(ApiKey.key_hash == _hash(presented)))
    if api_key is None:
        raise ApiKeyInvalid()
    now = _now()
    if api_key.revoked_at is not None:
        raise ApiKeyRevoked()
    if api_key.expires_at <= now:
        raise ApiKeyExpired(
            f"This API key expired on {api_key.expires_at:%Y-%m-%d}. Create a new one in IdentityHub settings."
        )
    user = await db.get(User, api_key.user_id)
    if user is None or not user.is_active:
        raise ApiKeyInvalid()
    permissions = _parse_permissions(api_key)

    if api_key.last_used_at is None or now - api_key.last_used_at >= LAST_USED_GRANULARITY:
        api_key.last_used_at = now
        await db.commit()
    return AuthenticatedKey(api_key, user, permissions)


def require_scope(auth: AuthenticatedKey, scope: Scope) -> None:
    if scope not in auth.permissions.scopes:
        raise ApiKeyScopeMissing(f"This API key doesn't have the '{scope.value}' permission.")


def ensure_project_allowed(auth: AuthenticatedKey, project_key: str) -> None:
    projects = auth.permissions.projects
    if projects != ALL_PROJECTS and project_key not in projects:
        raise ApiKeyProjectForbidden(
            f"This API key isn't allowed to create tickets in {project_key}. It is limited to: {', '.join(projects)}."
        )


# --- Helpers ----------------------------------------------------------------------------------


async def _owned_key(db: AsyncSession, user: User, key_id: uuid.UUID) -> ApiKey:
    # Filtering by owner means another user's key id is indistinguishable from a missing one.
    api_key = await db.scalar(select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id))
    if api_key is None:
        raise ApiKeyNotFound()
    return api_key


def _parse_permissions(api_key: ApiKey) -> ApiKeyPermissions:
    """Fail closed: a document this code can't fully understand grants nothing."""
    try:
        return ApiKeyPermissions.model_validate(api_key.permissions)
    except ValidationError as exc:
        raise ApiKeyPermissionsUnreadable() from exc


def _displayable_permissions(api_key: ApiKey) -> ApiKeyPermissions:
    # Listing must not break on a document this version can't validate (so the user can still see
    # and revoke the key); *using* the key fails closed in `authenticate`.
    try:
        return ApiKeyPermissions.model_validate(api_key.permissions)
    except ValidationError:
        return ApiKeyPermissions.model_construct(**api_key.permissions)


def _hash(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _active_filters(user_id: uuid.UUID, now: datetime) -> tuple:
    return (ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None), ApiKey.expires_at > now)


def _status(api_key: ApiKey, now: datetime) -> ApiKeyStatus:
    if api_key.revoked_at is not None:
        return "revoked"
    return "expired" if api_key.expires_at <= now else "active"


def _out(api_key: ApiKey, now: datetime) -> ApiKeyOut:
    return ApiKeyOut(
        id=api_key.id,
        name=api_key.name,
        notes=api_key.notes,
        prefix=api_key.prefix,
        permissions=_displayable_permissions(api_key),
        status=_status(api_key, now),
        created_at=api_key.created_at,
        expires_at=api_key.expires_at,
        last_used_at=api_key.last_used_at,
        revoked_at=api_key.revoked_at,
    )
