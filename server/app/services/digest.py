"""NHI Blog Digest: users subscribe Jira projects; a scheduled job files each new Oasis Security blog
post, with an AI summary, into every subscribed project.

Tickets are created by a dedicated bot account when one is configured (so they read as coming
from IdentityHub, not from whoever subscribed); otherwise by the project's earliest subscriber who
still has access, with their own Jira connection. Two rules keep the bot from widening anyone's
access:

* A project can only be subscribed if the user *and* the bot can create issues in it.
* At delivery time, at least one subscriber must still be able to create issues there; if nobody
  can any more, the project gets nothing.

What gets filed: each project has a watermark, the newest post already filed there, or (fresh
start) when its current subscriptions began. Every run files the recent posts published after the
watermark that aren't there yet, oldest first, so posts are never skipped. Each post is summarized
once and stored; every project and every later run reuses that summary.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import DigestDelivery, DigestPost, DigestSubscription, User
from app.db.session import session_scope
from app.digest.blog import BlogError, BlogPost, fetch_recent_posts
from app.digest.summarizers import Summarizer, SummaryError, choose_summarizer
from app.jira import adf, client
from app.jira.client import BotConnection, JiraTarget
from app.jira.errors import (
    JiraError,
    JiraForbidden,
    JiraNotFound,
    JiraReauthRequired,
    JiraUnavailable,
    JiraValidationError,
)
from app.schemas.findings import Project
from app.services.findings import APP_LABEL, browse_url, forget_issue_type, pick_issue_type
from app.services.jira_connection import get_connection, use_jira

log = logging.getLogger(__name__)

DIGEST_LABEL = "nhi-blog-digest"
MAX_SUBSCRIPTIONS = 20
BOT_CHECK_TTL_SECONDS = 300
MAX_TITLE = 255
# Catch-up bound: after a long outage, a project gets at most this many digests in one run.
MAX_POSTS_PER_PROJECT_PER_RUN = 5


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
    next_run_at: datetime | None = None  # the scheduler's planned time, jitter included
    # Set when subscriptions change during a run (which has already read them): run once more.
    rerun_requested: bool = False
    task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.lock.locked()


def bot_connection(settings: Settings) -> BotConnection | None:
    if not settings.digest_bot_configured:
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


def site_of(url: str) -> str:
    """Normalised site URL, e.g. https://acme.atlassian.net."""
    return f"https://{urlparse(url).netloc.lower()}"


async def unavailable_reason(db: AsyncSession, user: User, settings: Settings, runtime: DigestRuntime) -> str | None:
    """Why this user can't manage digest subscriptions right now, or None if they can."""
    if not settings.jira_configured:
        return "The Jira integration isn't configured on this server, so the blog digest can't file tickets."
    bot = bot_connection(settings)
    if bot is not None:
        await check_bot(settings, runtime)
        if runtime.bot_error:
            return runtime.bot_error
    try:
        async with use_jira(db, user) as conn:
            if bot is not None and site_of(conn.site_url) != site_of(bot.site_url):
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
    """Projects the user can create issues in, and (when a bot files the tickets) the bot too."""
    if reason := await unavailable_reason(db, user, settings, runtime):
        raise DigestUnavailable(reason)
    async with use_jira(db, user) as conn:
        mine = await client.search_projects(conn, query, 50)
    bot = bot_connection(settings)
    if bot is not None and mine:
        bots = {p["key"] for p in await client.search_projects(bot, None, 50, keys=[p["key"] for p in mine])}
        mine = [p for p in mine if p["key"] in bots]
    return [Project(id=p["id"], key=p["key"], name=p["name"]) for p in mine]


async def set_subscriptions(
    db: AsyncSession,
    user: User,
    settings: Settings,
    runtime: DigestRuntime,
    project_keys: list[str],
    send_latest_now: bool = False,
) -> bool:
    """Replace the user's subscriptions. Returns True if new projects asked for the latest post now."""
    if len(project_keys) > MAX_SUBSCRIPTIONS:
        raise DigestProjectsNotEligible(f"You can subscribe at most {MAX_SUBSCRIPTIONS} projects.")
    mine: dict[str, str] = {}
    site = ""
    if project_keys:
        if reason := await unavailable_reason(db, user, settings, runtime):
            raise DigestUnavailable(reason)
        async with use_jira(db, user) as conn:
            site = site_of(conn.site_url)
            mine = {p["key"]: p["name"] for p in await client.search_projects(conn, None, 50, keys=project_keys)}
        bot = bot_connection(settings)
        bots = {p["key"] for p in await client.search_projects(bot, None, 50, keys=project_keys)} if bot else None
        problems = []
        for key in project_keys:
            if key not in mine:
                problems.append(f"you can't create issues in {key}")
            elif bots is not None and key not in bots:
                problems.append(f"the IdentityHub bot can't create issues in {key} (a Jira admin can grant it access)")
        if problems:
            raise DigestProjectsNotEligible("Can't subscribe: " + "; ".join(problems) + ".")

    existing = {s.project_key for s in await db.scalars(select(DigestSubscription).where(DigestSubscription.user_id == user.id))}
    await db.execute(
        delete(DigestSubscription).where(
            DigestSubscription.user_id == user.id, DigestSubscription.project_key.not_in(project_keys)
        )
    )
    added = [key for key in project_keys if key not in existing]
    for key in added:
        db.add(
            DigestSubscription(
                user_id=user.id, site_url=site, project_key=key, project_name=mine[key], include_latest=send_latest_now
            )
        )
    await db.commit()
    return bool(added) and send_latest_now


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
            .where(DigestDelivery.site_url == sub.site_url, DigestDelivery.project_key == sub.project_key)
            .order_by(DigestDelivery.created_at.desc())
            .limit(1)
        )
        result.append((sub, latest))
    return result


# --- The run ---------------------------------------------------------------------------------

# A project is identified by its site and key: keys are only unique within a Jira site.
Target = tuple[str, str]  # (site_url, project_key)


async def run_digest(settings: Settings, runtime: DigestRuntime) -> LastRun:
    """File every new blog post in every subscribed project that hasn't received it yet."""
    if runtime.running:
        raise DigestAlreadyRunning()
    async with runtime.lock:
        while True:
            runtime.rerun_requested = False
            try:
                run = await _run(settings, runtime)
            except BlogError as exc:
                run = LastRun(finished_at=datetime.now(UTC), outcome="failed", error=str(exc))
            except Exception as exc:  # keep the scheduler alive, and say what happened
                log.exception("Blog digest run failed")
                run = LastRun(finished_at=datetime.now(UTC), outcome="failed", error=f"Unexpected error: {exc}")
            runtime.last_run = run
            log.info("Blog digest: %s%s", run.outcome, f" ({run.error})" if run.error else "")
            if not runtime.rerun_requested:
                return run


async def _run(settings: Settings, runtime: DigestRuntime) -> LastRun:
    if not settings.jira_configured:
        return LastRun(finished_at=datetime.now(UTC), outcome="disabled")
    bot = bot_connection(settings)
    if bot is not None:
        await check_bot(settings, runtime, force=True)
        if runtime.bot_error:
            return LastRun(finished_at=datetime.now(UTC), outcome="failed", error=runtime.bot_error)

    posts = await fetch_recent_posts(settings.digest_blog_url)  # oldest first
    newest = posts[-1]

    def done(outcome: str, error: str | None = None) -> LastRun:
        return LastRun(datetime.now(UTC), outcome, post_title=newest.title, post_url=newest.url, error=error)

    async with session_scope() as db:
        subs_by_target: dict[Target, list[DigestSubscription]] = {}
        for sub in await db.scalars(select(DigestSubscription).order_by(DigestSubscription.created_at)):
            subs_by_target.setdefault((sub.site_url, sub.project_key), []).append(sub)
        if not subs_by_target:
            return done("no subscribed projects")

        # Which posts each project is due: newer than its watermark and not filed there yet.
        plan: dict[Target, list[BlogPost]] = {}
        for target, subs in subs_by_target.items():
            watermark = await _watermark(db, target, subs, posts)
            filed_urls = set(await db.scalars(select(DigestPost.url).join(DigestDelivery).where(*_is_target(target))))
            due = [p for p in posts if p.published > watermark and p.url not in filed_urls]
            if due:
                plan[target] = due[:MAX_POSTS_PER_PROJECT_PER_RUN]
        if not plan:
            return done("up to date: no posts published since the last digest (or since subscribing)")

        stored, summary_errors = await _summaries(db, settings, [p for p in posts if any(p in due for due in plan.values())])

        filed: list[str] = []
        problems: list[str] = []
        for target, due in sorted(plan.items()):
            _, project_key = target
            entitled = await _entitled_subscribers(db, target, subs_by_target[target])
            if not entitled:
                problems.append(project_key)
                continue
            error = await _deliver(db, bot, target, due, stored, summary_errors, entitled[0], filed)
            # Subscribers who lost access keep their own message; the rest get this run's outcome.
            for sub in entitled:
                sub.last_error = error
            if error:
                problems.append(project_key)
        await db.commit()

    outcome = f"filed {len(filed)} ticket{'s' * (len(filed) != 1)} in {', '.join(sorted(set(filed)))}" if filed else "nothing filed"
    return done(outcome, f"Problems in {', '.join(problems)}; see the subscription errors." if problems else None)


async def _deliver(
    db: AsyncSession,
    bot: BotConnection | None,
    target: Target,
    due: list[BlogPost],
    stored: dict[str, DigestPost],
    summary_errors: dict[str, str],
    first_subscriber: DigestSubscription,
    filed: list[str],
) -> str | None:
    """File `due` posts (oldest first) in one project; returns the error that stopped it, if any.

    The bot files when configured. Otherwise the project's earliest subscriber who still has
    access files with their own Jira connection (the no-bot fallback).
    """
    _, project_key = target
    if bot is not None:
        return await _file_posts(db, bot, None, None, target, due, stored, summary_errors, filed)
    user = await db.get(User, first_subscriber.user_id)
    assert user is not None
    connection = await get_connection(db, user)
    filer_name = (connection.account_name if connection else None) or user.email
    try:
        async with use_jira(db, user) as conn:
            return await _file_posts(db, conn, user, filer_name, target, due, stored, summary_errors, filed)
    except JiraError as exc:
        return f"Couldn't file the digest in {project_key}: {exc.message}"


async def _file_posts(
    db: AsyncSession,
    jira: JiraTarget,
    filer: User | None,
    filer_name: str | None,
    target: Target,
    due: list[BlogPost],
    stored: dict[str, DigestPost],
    summary_errors: dict[str, str],
    filed: list[str],
) -> str | None:
    _, project_key = target
    for post in due:  # oldest first; stop at the first failure so the order is kept
        digest_post = stored.get(post.url)
        if digest_post is None:
            return f"The summary of '{post.title}' couldn't be generated: {summary_errors[post.url]}"
        try:
            await _file_ticket(db, jira, filer, filer_name, target, digest_post)
        except JiraError as exc:
            if filer is None and isinstance(exc, (JiraForbidden, JiraNotFound)):
                return f"The IdentityHub bot can no longer create issues in {project_key}. Ask a Jira admin to grant it access."
            return f"Couldn't file the digest in {project_key}: {exc.message}"
        filed.append(project_key)
    return None


def _is_target(target: Target) -> tuple:
    site_url, project_key = target
    return (DigestDelivery.site_url == site_url, DigestDelivery.project_key == project_key)


async def _watermark(
    db: AsyncSession, target: Target, subs: list[DigestSubscription], posts: list[BlogPost]
) -> datetime:
    """Posts published after this are due in the project: the newest post already filed there, or
    (fresh start) when the project's current subscriptions began, whichever is later."""
    subscribed_since = min(_starts_from(s, posts) for s in subs)
    last_filed = await db.scalar(select(func.max(DigestPost.published_at)).join(DigestDelivery).where(*_is_target(target)))
    return max(subscribed_since, last_filed) if last_filed else subscribed_since


def _starts_from(sub: DigestSubscription, posts: list[BlogPost]) -> datetime:
    """Fresh start: posts published after subscribing. With "send the latest post now", also the
    newest post that existed at that moment."""
    if sub.include_latest:
        earlier = [p.published for p in posts if p.published <= sub.created_at]
        if earlier:
            return max(earlier) - timedelta(microseconds=1)
    return sub.created_at


async def _summaries(
    db: AsyncSession, settings: Settings, posts: list[BlogPost]
) -> tuple[dict[str, DigestPost], dict[str, str]]:
    """Stored summaries for `posts`, generating (and saving) only the missing ones."""
    urls = [p.url for p in posts]
    stored = {d.url: d for d in await db.scalars(select(DigestPost).where(DigestPost.url.in_(urls)))}
    errors: dict[str, str] = {}
    summarizer: Summarizer | None = None
    for post in posts:
        if post.url in stored:
            continue
        try:
            summarizer = summarizer or await choose_summarizer(settings)
            text = await summarizer.summarize(post)
        except SummaryError as exc:
            errors[post.url] = str(exc)
            continue
        digest_post = DigestPost(
            url=post.url,
            title=post.title,
            published_at=post.published,
            summary=text,
            summarizer=summarizer.method,
            summarizer_description=summarizer.description,
        )
        db.add(digest_post)
        await db.commit()  # saved straight away: a later failure must not cost another model call
        stored[post.url] = digest_post
    return stored, errors


async def _entitled_subscribers(
    db: AsyncSession, target: Target, subs: list[DigestSubscription]
) -> list[DigestSubscription]:
    """Subscribers (earliest first) who can still create issues in the project on its site. The
    project gets the digest only if there is at least one; the others are told why they don't count."""
    site_url, project_key = target
    entitled = []
    for sub in subs:
        user = await db.get(User, sub.user_id)
        if user is None or not user.is_active:
            continue
        try:
            async with use_jira(db, user) as conn:
                if site_of(conn.site_url) != site_url:
                    sub.last_error = (
                        f"Your Jira connection is now to {conn.site_url}, not {site_url}. "
                        "Subscribe again to receive the digest there."
                    )
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
        entitled.append(sub)
    return entitled


async def _file_ticket(
    db: AsyncSession, jira: JiraTarget, filer: User | None, filer_name: str | None, target: Target, post: DigestPost
) -> None:
    site_url, project_key = target
    title = f"NHI Blog Digest: {post.title}"
    if len(title) > MAX_TITLE:
        title = title[: MAX_TITLE - 1] + "…"
    filed_by = (
        "Filed by IdentityHub's NHI Blog Digest"
        if filer is None
        else f"Filed by IdentityHub's NHI Blog Digest using {filer_name}'s Jira connection, because no digest bot account is configured"
    )
    description = adf.document(
        *adf.plain_paragraphs(post.summary),
        adf.paragraph(adf.text("Source: ", "strong"), adf.link(post.url, post.url)),
        adf.paragraph(adf.text("Published: ", "strong"), adf.text(f"{post.published_at:%Y-%m-%d}")),
        adf.paragraph(adf.text(f"{filed_by}. Summary: {post.summarizer_description}.", "em")),
    )
    try:
        issue = await client.create_issue(
            jira,
            {
                "project": {"key": project_key},
                "issuetype": {"id": await pick_issue_type(jira, project_key)},
                "summary": title,
                "description": description,
                "labels": [APP_LABEL, DIGEST_LABEL],
            },
        )
    except JiraValidationError:
        forget_issue_type(jira, project_key)
        raise
    db.add(
        DigestDelivery(
            post_id=post.id,
            site_url=site_url,
            project_key=project_key,
            issue_key=issue["key"],
            issue_url=browse_url(jira, issue["key"]),
            filed_by_user_id=filer.id if filer else None,
        )
    )
    await db.commit()  # recorded per ticket, so a crash later in the run can't cause a duplicate


# --- Scheduling -------------------------------------------------------------------------------


def next_run_at(daily_at: str, now: datetime) -> datetime:
    """The next occurrence of `daily_at` (HH:MM, UTC) after `now`."""
    hour, minute = (int(part) for part in daily_at.split(":"))
    candidate = now.astimezone(UTC).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


def planned_run_at(daily_at: str, jitter_minutes: int, now: datetime, rng: random.Random | None = None) -> datetime:
    """The next run: DIGEST_DAILY_AT plus a random offset of up to `jitter_minutes`."""
    offset = (rng or random).uniform(0, jitter_minutes * 60)
    return next_run_at(daily_at, now) + timedelta(seconds=offset)


async def schedule(settings: Settings, runtime: DigestRuntime, startup_delay_seconds: float = 60) -> None:
    """A catch-up run shortly after startup (in case the server was down at the scheduled time;
    cheap, since already-filed posts and stored summaries are reused), then daily at
    DIGEST_DAILY_AT UTC plus jitter. In-process: exactly one instance of the app should run it."""
    await asyncio.sleep(startup_delay_seconds)
    while True:
        try:
            await run_digest(settings, runtime)
        except DigestAlreadyRunning:
            pass
        runtime.next_run_at = planned_run_at(settings.digest_daily_at, settings.digest_jitter_minutes, datetime.now(UTC))
        await asyncio.sleep(max((runtime.next_run_at - datetime.now(UTC)).total_seconds(), 1))
