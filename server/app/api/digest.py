"""NHI Blog Digest: status, project subscriptions, and "send the latest post" per project."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.users import current_active_user
from app.core.config import get_settings
from app.core.rate_limit import RateLimit
from app.db.models import User
from app.db.session import get_db
from app.digest.summarizers import SummaryError, choose_summarizer
from app.schemas.findings import PROJECT_KEY_PATTERN, ProjectKey
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
    unavailable_reason: str | None = Field(description="Why the user can't manage subscriptions right now")
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
        unavailable_reason=reason,
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


class SubscriptionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_keys: Annotated[list[ProjectKey], Field(max_length=digest.MAX_SUBSCRIPTIONS)]

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
    await digest.set_subscriptions(db, user, get_settings(), runtime, body.project_keys)
    return await get_status(user, db, runtime)


class SentLatestOut(BaseModel):
    filed: int = Field(description="Tickets created now; 0 if the latest post was already filed in the project")
    ticket: LastTicket = Field(description="The project's ticket for the latest post")


@router.post(
    "/subscriptions/{project_key}/send-latest",
    summary="File the latest blog post in a subscribed project now",
    dependencies=[Depends(RateLimit("digest_send_latest", "5/minute"))],
)
async def send_latest(
    project_key: Annotated[str, Path(pattern=PROJECT_KEY_PATTERN)],
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
    runtime: DigestRuntime = Depends(_runtime),
) -> SentLatestOut:
    """Also files any earlier posts the project hasn't received yet, so none is skipped. Does
    nothing (and says so) if the latest post is already there."""
    sent = await digest.send_latest(db, user, get_settings(), runtime, project_key)
    d = sent.latest
    return SentLatestOut(
        filed=sent.filed,
        ticket=LastTicket(key=d.issue_key, url=d.issue_url, post_title=d.post.title, created_at=d.created_at),
    )
