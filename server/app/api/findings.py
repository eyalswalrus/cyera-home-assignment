from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.users import current_active_user
from app.db.models import FindingSource, User
from app.db.session import get_db
from app.schemas.findings import PROJECT_KEY_PATTERN, FindingCreate, FindingCreated, RecentTicket
from app.services import findings

router = APIRouter(prefix="/findings", tags=["findings"])


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create an NHI finding ticket in Jira")
async def create_finding(
    body: FindingCreate,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> FindingCreated:
    return await findings.create_finding(db, user, body, FindingSource.UI)


@router.get("/recent", summary="The 10 newest tickets IdentityHub created in a project")
async def recent_findings(
    project_key: Annotated[str, Query(pattern=PROJECT_KEY_PATTERN)],
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[RecentTicket]:
    return await findings.recent_findings(db, user, project_key)
