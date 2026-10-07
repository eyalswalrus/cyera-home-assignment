"""NHI Blog Digest: status, project subscriptions, and a manual "run now"."""

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.users import current_active_user
from app.core.config import Settings, get_settings
from app.core.rate_limit import RateLimit
from app.db.models import User
from app.db.session import get_db
from app.digest.summarizers import SummaryError, choose_summarizer
from app.schemas.findings import Project, ProjectKey
from app.services import digest
from app.services.digest import DigestRuntime

router = APIRouter(prefix="/digest", tags=["blog digest"])


def _runtime(request: Request) -> DigestRuntime:
    return request.app.state.digest


class LastTicket(BaseModel):
    key: str
    url: str
    post_title: str
    created_at: datetime


class SubscriptionOut(BaseModel):
    project_key: str
    project_name: str
    last_error: str | None
    last_ticket: LastTicket | None


class LastRunOut(BaseModel):
    finished_at: datetime
    outcome: str
    post_title: str | None
    post_url: str | None
    error: str | None


class DigestStatus(BaseModel):
    configured: bool = Field(description="The digest can run (the Jira integration is configured)")
    filed_by: Literal["bot", "subscriber"] = Field(
        description="Who files the tickets: the digest bot account, or (no bot configured) a subscriber's own Jira connection"
    )
    unavailable_reason: str | None = Field(description="Why the user can't manage subscriptions right now")
    bot_account: str | None
    site_url: str | None
    summarizer: str | None
    daily_at_utc: str = Field(description="Daily run time, HH:MM UTC")
    jitter_minutes: int = Field(description="Runs start up to this many minutes after daily_at_utc")
    next_run_at: datetime | None
    running: bool
    last_run: LastRunOut | None
    subscriptions: list[SubscriptionOut]


@router.get("", summary="Digest status and your subscriptions")
async def get_status(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
    runtime: DigestRuntime = Depends(_runtime),
) -> DigestStatus:
    settings = get_settings()
    reason = await digest.unavailable_reason(db, user, settings, runtime)
    try:
        summarizer = (await choose_summarizer(settings)).description if settings.jira_configured else None
    except SummaryError as exc:
        summarizer = f"misconfigured: {exc}"
    subs = await digest.list_subscriptions(db, user)
    last = runtime.last_run
    return DigestStatus(
        configured=settings.jira_configured,
        filed_by="bot" if settings.digest_bot_configured else "subscriber",
        unavailable_reason=reason,
        bot_account=runtime.bot_account if settings.digest_bot_configured else None,
        site_url=settings.digest_jira_site_url if settings.digest_bot_configured else None,
        summarizer=summarizer,
        daily_at_utc=settings.digest_daily_at,
        next_run_at=(runtime.next_run_at or digest.next_run_at(settings.digest_daily_at, datetime.now(UTC)))
        if settings.jira_configured
        else None,
        jitter_minutes=settings.digest_jitter_minutes,
        running=runtime.running,
        last_run=LastRunOut(**last.__dict__) if last else None,
        subscriptions=[
            SubscriptionOut(
                project_key=sub.project_key,
                project_name=sub.project_name,
                last_error=sub.last_error,
                last_ticket=LastTicket(
                    key=d.issue_key, url=d.issue_url, post_title=d.post.title, created_at=d.created_at
                )
                if d
                else None,
            )
            for sub, d in subs
        ],
    )


@router.get("/projects", summary="Projects you and the digest bot can both create issues in")
async def eligible_projects(
    query: Annotated[str | None, Query(max_length=100)] = None,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
    runtime: DigestRuntime = Depends(_runtime),
) -> list[Project]:
    return await digest.eligible_projects(db, user, get_settings(), runtime, query)


class SubscriptionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_keys: Annotated[list[ProjectKey], Field(max_length=digest.MAX_SUBSCRIPTIONS)]
    send_latest_now: bool = Field(
        default=False,
        description="For projects added in this request: also file the current latest post, and run now.",
    )

    @field_validator("project_keys", mode="before")
    @classmethod
    def _normalise(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return list(dict.fromkeys(v.strip().upper() if isinstance(v, str) else v for v in value))


@router.put("/subscriptions", summary="Set which projects receive the digest")
async def set_subscriptions(
    body: SubscriptionsIn,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
    runtime: DigestRuntime = Depends(_runtime),
) -> DigestStatus:
    settings = get_settings()
    if await digest.set_subscriptions(db, user, settings, runtime, body.project_keys, body.send_latest_now):
        _start_run(settings, runtime)
    return await get_status(user, db, runtime)


def _start_run(settings: Settings, runtime: DigestRuntime) -> bool:
    """Start a background run unless one is already going (it will pick the change up anyway on
    its next pass). Returns whether a run was started."""
    if runtime.running:
        return False
    runtime.task = asyncio.create_task(digest.run_digest(settings, runtime))
    return True


@router.post(
    "/run",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run the digest now (in the background)",
    dependencies=[Depends(RateLimit("digest_run", "3/minute"))],
)
async def run_now(user: User = Depends(current_active_user), runtime: DigestRuntime = Depends(_runtime)) -> dict:
    settings = get_settings()
    if not settings.jira_configured:
        raise digest.DigestUnavailable("The Jira integration isn't configured on this server.")
    # Already-filed posts are skipped per project, so running again never duplicates tickets.
    if not _start_run(settings, runtime):
        raise digest.DigestAlreadyRunning()
    return {"status": "started"}
