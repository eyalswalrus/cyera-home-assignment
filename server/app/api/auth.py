from fastapi import APIRouter, Depends

from app.auth.users import UserCreate, UserRead, auth_backend, current_active_user, fastapi_users
from app.core.rate_limit import RateLimit
from app.db.models import User

# Per-IP throttle on the credential-handling endpoints (login, logout, register).
_auth_rate_limit = [Depends(RateLimit("auth", "10/minute"))]

router = APIRouter(prefix="/auth", tags=["auth"])

# POST /auth/login (form: username, password) -> 204 + session cookie
# POST /auth/logout                             -> 204, session row deleted
router.include_router(fastapi_users.get_auth_router(auth_backend), dependencies=_auth_rate_limit)
# POST /auth/register (JSON: email, password)   -> 201
router.include_router(fastapi_users.get_register_router(UserRead, UserCreate), dependencies=_auth_rate_limit)


@router.get("/me")
async def me(user: User = Depends(current_active_user)) -> UserRead:
    return UserRead.model_validate(user, from_attributes=True)
