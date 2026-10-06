"""App authentication, built on fastapi-users.

Sessions are opaque random tokens stored server-side (`accesstoken` table) and carried in an
httpOnly cookie. Logging out deletes the row, so a stolen cookie stops working immediately -
unlike a stateless JWT, which stays valid until it expires.
"""

import uuid
from collections.abc import AsyncIterator

from fastapi import Depends
from fastapi_users import BaseUserManager, FastAPIUsers, InvalidPasswordException, UUIDIDMixin, schemas
from fastapi_users.authentication import AuthenticationBackend, CookieTransport
from fastapi_users.authentication.strategy.db import AccessTokenDatabase, DatabaseStrategy
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from fastapi_users_db_sqlalchemy.access_token import SQLAlchemyAccessTokenDatabase
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import AccessToken, User
from app.db.session import get_db

SESSION_COOKIE_NAME = "identityhub_session"
SESSION_LIFETIME_SECONDS = 8 * 60 * 60  # absolute lifetime: one working day
MIN_PASSWORD_LENGTH = 12


class UserRead(schemas.BaseUser[uuid.UUID]):
    pass


class UserCreate(schemas.BaseUserCreate):
    pass


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    # Password-reset/verification flows are not exposed (no email delivery in this POC), but
    # fastapi-users requires these secrets to be set.
    @property
    def reset_password_token_secret(self) -> str:  # type: ignore[override]
        return get_settings().secret_key.get_secret_value()

    @property
    def verification_token_secret(self) -> str:  # type: ignore[override]
        return get_settings().secret_key.get_secret_value()

    async def validate_password(self, password: str, user: UserCreate | User) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise InvalidPasswordException(
                reason=f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
            )
        if user.email.split("@", 1)[0].lower() in password.lower():
            raise InvalidPasswordException(reason="Password must not contain your email address.")


async def get_user_db(db: AsyncSession = Depends(get_db)) -> AsyncIterator[SQLAlchemyUserDatabase]:
    yield SQLAlchemyUserDatabase(db, User)


async def get_user_manager(user_db: SQLAlchemyUserDatabase = Depends(get_user_db)) -> AsyncIterator[UserManager]:
    yield UserManager(user_db)


async def get_access_token_db(
    db: AsyncSession = Depends(get_db),
) -> AsyncIterator[SQLAlchemyAccessTokenDatabase[AccessToken]]:
    yield SQLAlchemyAccessTokenDatabase(db, AccessToken)


def get_database_strategy(
    access_token_db: AccessTokenDatabase[AccessToken] = Depends(get_access_token_db),
) -> DatabaseStrategy:
    return DatabaseStrategy(access_token_db, lifetime_seconds=SESSION_LIFETIME_SECONDS)


auth_backend = AuthenticationBackend(
    name="session",
    transport=CookieTransport(
        cookie_name=SESSION_COOKIE_NAME,
        cookie_max_age=SESSION_LIFETIME_SECONDS,
        cookie_secure=get_settings().cookie_secure,
        cookie_httponly=True,  # not readable from JavaScript, so XSS can't exfiltrate it
        cookie_samesite="lax",  # not sent on cross-site POSTs; CSRF tokens cover the rest
    ),
    get_strategy=get_database_strategy,
)

fastapi_users = FastAPIUsers[User, uuid.UUID](get_user_manager, [auth_backend])

# Dependency for routes that require a logged-in user; responds 401 otherwise.
current_active_user = fastapi_users.current_user(active=True)
