from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import get_settings

router = APIRouter(tags=["meta"])


class HealthResponse(BaseModel):
    status: str
    jira_configured: bool


@router.get("/health")
async def health() -> HealthResponse:
    return HealthResponse(status="ok", jira_configured=get_settings().jira_configured)
