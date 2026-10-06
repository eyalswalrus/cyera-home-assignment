"""Managing API keys from the UI (session-authenticated)."""

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.users import current_active_user
from app.db.models import User
from app.db.session import get_db
from app.schemas.api_keys import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, ApiKeyUpdate
from app.services import api_keys

router = APIRouter(prefix="/api-keys", tags=["api keys"])


@router.get("", summary="List your API keys")
async def list_keys(user: User = Depends(current_active_user), db: AsyncSession = Depends(get_db)) -> list[ApiKeyOut]:
    return await api_keys.list_keys(db, user)


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create an API key (the key is returned once)")
async def create_key(
    body: ApiKeyCreate, user: User = Depends(current_active_user), db: AsyncSession = Depends(get_db)
) -> ApiKeyCreated:
    return await api_keys.create_key(db, user, body)


@router.patch("/{key_id}", summary="Edit an API key's notes (permissions can't be changed)")
async def update_key(
    key_id: uuid.UUID,
    body: ApiKeyUpdate,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyOut:
    return await api_keys.update_notes(db, user, key_id, body)


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Revoke an API key")
async def revoke_key(
    key_id: uuid.UUID, user: User = Depends(current_active_user), db: AsyncSession = Depends(get_db)
) -> None:
    await api_keys.revoke_key(db, user, key_id)
