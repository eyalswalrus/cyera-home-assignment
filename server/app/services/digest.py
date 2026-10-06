"""NHI Blog Digest: users subscribe Jira projects; a scheduled job files each new Oasis Security blog
post, with an AI summary, into every subscribed project.

Tickets are created by a dedicated bot account (so they read as coming from IdentityHub, not from
whoever subscribed), configured by the deployer. Two rules keep the bot from widening anyone's
access:

* A project can only be subscribed if the user *and* the bot can create issues in it.
* At delivery time, at least one subscriber must still be able to create issues there; if nobody
  can any more, the project gets nothing.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import DigestDelivery, DigestSubscription, User
from app.db.session import session_scope
from app.digest.blog import BlogError, BlogPost, fetch_latest_post
from app.digest.summarizers import Summarizer, SummaryError, choose_summarizer
from app.jira import adf, client
from app.jira.client import BotConnection, JiraTarget
from app.jira.errors import JiraError, JiraForbidden, JiraNotFound, JiraReauthRequired, JiraUnavailable
from app.schemas.findings import Project
from app.services.findings import APP_LABEL, browse_url, pick_issue_type
from app.services.jira_connection import use_jira

log = logging.getLogger(__name__)

DIGEST_LABEL = "nhi-blog-digest"
MAX_SUBSCRIPTIONS = 20
BOT_CHECK_TTL_SECONDS = 300
MAX_TITLE = 255


# --- Errors -----------------------------------------------------------------------------------


class DigestUnavailable(AppError):
    status_code = 409
    code = "digest_unavailable"


class DigestProjectsNotEligible(AppError):
    status_code = 400
    code = "digest_projects_not_eligible"


class DigestAlreadyRunning(AppError):
    status_code = 409
    code = "digest_already_running"
    message = "The digest is already running. Check back in a minute."


# --- Runtime state (one per process) ----------------------------------------------------------


@dataclass
class LastRun:
    finished_at: datetime
    outcome: str
    post_title: str | None = None
    post_url: str | None = None
    error: str | None = None


@dataclass
class DigestRuntime:
    """In-process state: the bot's health (re-checked every few minutes) and the last run."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    bot_account: str | None = None
    bot_error: str | None = None
    bot_checked_at: float = 0.0
    last_run: LastRun | None = None
    task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.lock.locked()


def bot_connection(settings: Settings) -> BotConnection | None:
    if not settings.digest_configured:
        return None
    assert settings.digest_jira_site_url and settings.digest_jira_email and settings.digest_jira_api_token
    return BotConnection(
        settings.digest_jira_site_url, settings.digest_jira_email, settings.digest_jira_api_token.get_secret_value()
    )


async def check_bot(settings: Settings, runtime: DigestRuntime, *, force: bool = False) -> None:
    """Verify the bot credentials (cached for a few minutes) and remember who the bot is."""
    bot = bot_connection(settings)
    if bot is None or (not force and time.monotonic() - runtime.bot_checked_at < BOT_CHECK_TTL_SECONDS):
        return
    try:
        account = await client.get_account(bot)
        runtime.bot_account, runtime.bot_error = account.get("displayName") or settings.digest_jira_email, None
    except JiraError as exc:
        runtime.bot_error = _bot_error_message(settings, bot, exc)
        log.warning(runtime.bot_error)
    runtime.bot_checked_at = time.monotonic()


def _bot_error_message(settings: Settings, bot: BotConnection, exc: JiraError) -> str:
    bot_account = f"the digest bot account ({settings.digest_jira_email})"
    who = bot_account[0].upper() + bot_account[1:]
    if isinstance(exc, JiraNotFound):
        return f"{who} couldn't find a Jira site at {bot.site_url}. Check DIGEST_JIRA_SITE_URL."
    if isinstance(exc, (JiraReauthRequired, JiraForbidden)):
        return f"Jira rejected the credentials of {bot_account}. Check DIGEST_JIRA_EMAIL and DIGEST_JIRA_API_TOKEN."
    if isinstance(exc, JiraUnavailable):
        return f"{who} couldn't reach {bot.site_url}. Jira may be down; this is retried automatically."
    return f"{who} couldn't sign in to {bot.site_url}: {exc.message}"


# --- What a user can do -----------------------------------------------------------------------


def _same_site(a: str, b: str) -> bool:
    return urlparse(a).netloc.lower() == urlparse(b).netloc.lower()


async def unavailable_reason(db: AsyncSession, user: User, settings: Settings, runtime: DigestRuntime) -> str | None:
    """Why this user can't manage digest subscriptions right now, or None if they can."""
    bot = bot_connection(settings)
    if bot is None:
        return "The blog digest hasn't been set up on this server. An administrator needs to configure the digest bot account."
    await check_bot(settings, runtime)
    if runtime.bot_error:
        return runtime.bot_error
    try:
        async with use_jira(db, user) as conn:
            if not _same_site(conn.site_url, bot.site_url):
                return (
                    f"The digest bot posts to {bot.site_url}, but your Jira connection is to {conn.site_url}. "
                    "Connect the same Jira site to subscribe."
                )
    except JiraError as exc:
        return exc.message
    return None


async def eligible_projects(
    db: AsyncSession, user: User, settings: Settings, runtime: DigestRuntime, query: str | None
) -> list[Project]:
    """Projects that both the user and the bot can create issues in."""
    if reason := await unavailable_reason(db, user, settings, runtime):
        raise DigestUnavailable(reason)
    bot = bot_connection(settings)
    assert bot is not None
    async with use_jira(db, user) as conn:
        mine = await client.search_projects(conn, query, 50)
    if not mine:
        return []
    bots = {p["key"] for p in await client.search_projects(bot, None, 50, keys=[p["key"] for p in mine])}
    return [Project(id=p["id"], key=p["key"], name=p["name"]) for p in mine if p["key"] in bots]


async def set_subscriptions(
    db: AsyncSession, user: User, settings: Settings, runtime: DigestRuntime, project_keys: list[str]
) -> None:
    if len(project_keys) > MAX_SUBSCRIPTIONS:
        raise DigestProjectsNotEligible(f"You can subscribe at most {MAX_SUBSCRIPTIONS} projects.")
    if project_keys:
        if reason := await unavailable_reason(db, user, settings, runtime):
            raise DigestUnavailable(reason)
        bot = bot_connection(settings)
        assert bot is not None
        async with use_jira(db, user) as conn:
            mine = {p["key"]: p["name"] for p in await client.search_projects(conn, None, 50, keys=project_keys)}
        bots = {p["key"] for p in await client.search_projects(bot, None, 50, keys=project_keys)}
        problems = []
        for key in project_keys:
            if key not in mine:
                problems.append(f"you can't create issues in {key}")
            elif key not in bots:
                problems.append(f"the IdentityHub bot can't create issues in {key} (a Jira admin can grant it access)")
        if problems:
            raise DigestProjectsNotEligible("Can't subscribe: " + "; ".join(problems) + ".")

    existing = {s.project_key: s for s in await db.scalars(select(DigestSubscription).where(DigestSubscription.user_id == user.id))}
    await db.execute(
        delete(DigestSubscription).where(
            DigestSubscription.user_id == user.id, DigestSubscription.project_key.not_in(project_keys)
        )
    )
    for key in project_keys:
        if key not in existing:
            db.add(DigestSubscription(user_id=user.id, project_key=key, project_name=mine[key]))
    await db.commit()


async def list_subscriptions(db: AsyncSession, user: User) -> list[tuple[DigestSubscription, DigestDelivery | None]]:
    subs = (
        await db.scalars(
            select(DigestSubscription).where(DigestSubscription.user_id == user.id).order_by(DigestSubscription.project_key)
        )
    ).all()
    result = []
    for sub in subs:
        latest = await db.scalar(
            select(DigestDelivery)
            .where(DigestDelivery.project_key == sub.project_key)
            .order_by(DigestDelivery.created_at.desc())
            .limit(1)
        )
        result.append((sub, latest))
    return result


# --- The run ----------------------------------------------------------------------------------


async def run_digest(settings: Settings, runtime: DigestRuntime) -> LastRun:
    """File the newest blog post in every subscribed project that hasn't received it yet."""
    if runtime.running:
        raise DigestAlreadyRunning()
    async with runtime.lock:
        try:
            run = await _run(settings, runtime)
        except (BlogError, SummaryError) as exc:
            run = LastRun(finished_at=datetime.now(UTC), outcome="failed", error=str(exc))
        except Exception as exc:  # keep the scheduler alive, and say what happened
            log.exception("Blog digest run failed")
            run = LastRun(finished_at=datetime.now(UTC), outcome="failed", error=f"Unexpected error: {exc}")
        runtime.last_run = run
        log.info("Blog digest: %s%s", run.outcome, f" ({run.error})" if run.error else "")
        return run


async def _run(settings: Settings, runtime: DigestRuntime) -> LastRun:
    bot = bot_connection(settings)
    if bot is None:
        return LastRun(finished_at=datetime.now(UTC), outcome="disabled")
    await check_bot(settings, runtime, force=True)
    if runtime.bot_error:
        return LastRun(finished_at=datetime.now(UTC), outcome="failed", error=runtime.bot_error)

    post = await fetch_latest_post(settings.digest_blog_url)

    def done(outcome: str, error: str | None = None) -> LastRun:
        return LastRun(datetime.now(UTC), outcome, post_title=post.title, post_url=post.url, error=error)

    async with session_scope() as db:
        delivered = set(await db.scalars(select(DigestDelivery.project_key).where(DigestDelivery.post_url == post.url)))
        pending: dict[str, list[DigestSubscription]] = {}
        for sub in await db.scalars(select(DigestSubscription)):
            if sub.project_key not in delivered:
                pending.setdefault(sub.project_key, []).append(sub)
        if not pending:
            return done("up to date")

        summarizer = await choose_summarizer(settings)
        summary = await summarizer.summarize(post)  # once, however many projects

        filed, failed = [], []
        for project_key, subs in sorted(pending.items()):
            if not await _still_entitled(db, bot, project_key, subs):
                failed.append(project_key)
                continue
            try:
                await _file_ticket(db, bot, project_key, post, summary, summarizer)
            except JiraError as exc:
                message = (
                    f"The IdentityHub bot can no longer create issues in {project_key}. Ask a Jira admin to grant it access."
                    if isinstance(exc, (JiraForbidden, JiraNotFound))
                    else f"Couldn't file the digest in {project_key}: {exc.message}"
                )
                for sub in subs:
                    sub.last_error = message
                failed.append(project_key)
                continue
            for sub in subs:
                sub.last_error = None
            filed.append(project_key)
        await db.commit()

    outcome = f"filed in {', '.join(filed)}" if filed else "nothing filed"
    return done(outcome, f"Skipped {', '.join(failed)}; see the subscription errors." if failed else None)


async def _still_entitled(db: AsyncSession, bot: BotConnection, project_key: str, subs: list[DigestSubscription]) -> bool:
    """True if at least one subscriber can still create issues in the project on the bot's site."""
    entitled = False
    for sub in subs:
        user = await db.get(User, sub.user_id)
        if user is None or not user.is_active:
            continue
        try:
            async with use_jira(db, user) as conn:
                if not _same_site(conn.site_url, bot.site_url):
                    sub.last_error = "Your Jira connection is to a different site than the digest bot."
                    continue
                if not await client.search_projects(conn, None, 1, keys=[project_key]):
                    sub.last_error = f"You can no longer create issues in {project_key}, so the digest wasn't filed there."
                    continue
        except JiraReauthRequired:
            sub.last_error = f"Reconnect Jira in Settings to keep receiving the digest in {project_key}."
            continue
        except JiraError as exc:
            sub.last_error = exc.message
            continue
        entitled = True
    return entitled


async def _file_ticket(
    db: AsyncSession, bot: JiraTarget, project_key: str, post: BlogPost, summary: str, summarizer: Summarizer
) -> None:
    title = f"NHI Blog Digest: {post.title}"
    if len(title) > MAX_TITLE:
        title = title[: MAX_TITLE - 1] + "…"
    description = adf.document(
        *adf.plain_paragraphs(summary),
        adf.paragraph(adf.text("Source: ", "strong"), adf.link(post.url, post.url)),
        adf.paragraph(adf.text("Published: ", "strong"), adf.text(f"{post.published:%Y-%m-%d}")),
        adf.paragraph(adf.text(f"Filed by IdentityHub's NHI Blog Digest. Summary: {summarizer.description}.", "em")),
    )
    issue = await client.create_issue(
        bot,
        {
            "project": {"key": project_key},
            "issuetype": {"id": await pick_issue_type(bot, project_key)},
            "summary": title,
            "description": description,
            "labels": [APP_LABEL, DIGEST_LABEL],
        },
    )
    db.add(
        DigestDelivery(
            post_url=post.url,
            post_title=post.title,
            project_key=project_key,
            issue_key=issue["key"],
            issue_url=browse_url(bot, issue["key"]),
            summarizer=summarizer.method,
        )
    )


# --- Scheduling -------------------------------------------------------------------------------


async def schedule(settings: Settings, runtime: DigestRuntime, first_delay_seconds: float = 60) -> None:
    """Run the digest shortly after startup, then every DIGEST_INTERVAL_HOURS. Runs in-process: one
    instance of the app must run it (see DESIGN.md for multi-replica deployments)."""
    await asyncio.sleep(first_delay_seconds)
    while True:
        try:
            await run_digest(settings, runtime)
        except DigestAlreadyRunning:
            pass
        await asyncio.sleep(settings.digest_interval_hours * 3600)
