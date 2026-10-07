from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import get_settings

router = APIRouter(tags=["meta"])


class HealthResponse(BaseModel):
    status: str
    jira_configured: bool
    digest_bot_configured: bool


@router.get("/health")
async def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(status="ok", jira_configured=settings.jira_configured, digest_bot_configured=settings.digest_bot_configured)
