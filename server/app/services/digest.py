"""NHI Blog Digest: users subscribe Jira projects; a scheduled job files each new Oasis Security blog
post, with an AI summary, into every subscribed project.

Tickets are filed with the project's earliest subscriber's own Jira connection, among those who
can still create issues there (so Jira enforces a real user's permissions, and the ticket says
whose connection filed it). If nobody subscribed can any more, the project gets nothing. A
production deployment would file as the app instead; see DESIGN.md section 9.

What gets filed: each project has a watermark, the newest post already filed there, or (fresh
start) when its current subscriptions began. Every run files the recent posts published after the
watermark that aren't there yet, oldest first, so posts are never skipped. Each post is summarized
once and stored; every project and every later run reuses that summary.
"""

import asyncio
import logging
import random
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
from app.jira.client import ActiveConnection
from app.jira.errors import (
    JiraError,
    JiraReauthRequired,
    JiraValidationError,
)
from app.services.findings import APP_LABEL, browse_url, forget_issue_type, pick_issue_type
from app.services.jira_connection import get_connection, use_jira

log = logging.getLogger(__name__)

DIGEST_LABEL = "nhi-blog-digest"
MAX_SUBSCRIPTIONS = 20
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


class DigestNotSubscribed(AppError):
    status_code = 404
    code = "digest_not_subscribed"


class DigestBlogUnavailable(AppError):
    status_code = 502
    code = "digest_blog_unavailable"


class DigestFilingFailed(AppError):
    status_code = 502
    code = "digest_filing_failed"


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
    """In-process state: the run lock, the last run and the next planned one."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_run: LastRun | None = None
    next_run_at: datetime | None = None  # the scheduler's planned time, jitter included
    # Set when subscriptions change during a run (which has already read them): run once more.
    rerun_requested: bool = False
    task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.lock.locked()


# --- What a user can do -----------------------------------------------------------------------


def site_of(url: str) -> str:
    """Normalised site URL, e.g. https://acme.atlassian.net."""
    return f"https://{urlparse(url).netloc.lower()}"


async def unavailable_reason(db: AsyncSession, user: User, settings: Settings, runtime: DigestRuntime) -> str | None:
    """Why this user can't manage digest subscriptions right now, or None if they can."""
    if not settings.jira_configured:
        return "The Jira integration isn't configured on this server, so the blog digest can't file tickets."
    try:
        async with use_jira(db, user):
            pass
    except JiraError as exc:
        return exc.message
    return None


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
        if missing := [key for key in project_keys if key not in mine]:
            raise DigestProjectsNotEligible(f"Can't subscribe: you can't create issues in {', '.join(missing)}.")

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


@dataclass
class SentLatest:
    filed: int  # tickets created by this request (0: the latest post was already there)
    latest: DigestDelivery  # the project's ticket for the newest post


async def send_latest(
    db: AsyncSession, user: User, settings: Settings, runtime: DigestRuntime, project_key: str
) -> SentLatest:
    """File the blog's newest post in one of the user's subscribed projects now, along with any
    earlier posts still due there (so none is skipped). Shares the run lock, so it can't race the
    scheduled run into filing a post twice."""
    sub = await db.scalar(
        select(DigestSubscription).where(DigestSubscription.user_id == user.id, DigestSubscription.project_key == project_key)
    )
    if sub is None:
        raise DigestNotSubscribed(f"You aren't subscribed to the digest in {project_key}.")
    if reason := await unavailable_reason(db, user, settings, runtime):
        raise DigestUnavailable(reason)
    if runtime.running:
        raise DigestAlreadyRunning("The digest is running right now. Try again in a minute.")
    async with runtime.lock:
        try:
            posts = await fetch_recent_posts(settings.digest_blog_url)  # oldest first
        except BlogError as exc:
            raise DigestBlogUnavailable(f"Couldn't read the Oasis Security blog: {exc}") from exc
        newest = posts[-1]
        target: Target = (sub.site_url, project_key)
        all_subs = (
            await db.scalars(
                select(DigestSubscription)
                .where(DigestSubscription.site_url == sub.site_url, DigestSubscription.project_key == project_key)
                .order_by(DigestSubscription.created_at)
            )
        ).all()
        watermark = await _watermark(db, target, list(all_subs), posts)
        filed_urls = set(await db.scalars(select(DigestPost.url).join(DigestDelivery).where(*_is_target(target))))
        due = [p for p in posts if (p.published > watermark or p is newest) and p.url not in filed_urls]
        due = due[-MAX_POSTS_PER_PROJECT_PER_RUN:]  # keeps the newest

        filed: list[str] = []
        if due:
            if not await _entitled_subscribers(db, target, [sub]):
                await db.commit()
                raise DigestFilingFailed(sub.last_error or f"The digest can't be filed in {project_key}.")
            stored, summary_errors = await _summaries(db, settings, due)
            error = await _deliver(db, target, due, stored, summary_errors, sub, filed)
            sub.last_error = error
            await db.commit()
            if error:
                raise DigestFilingFailed(error)

        latest = await db.scalar(
            select(DigestDelivery)
            .join(DigestPost)
            .where(*_is_target(target), DigestPost.url == newest.url)
        )
        assert latest is not None
        return SentLatest(filed=len(filed), latest=latest)


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
            error = await _deliver(db, target, due, stored, summary_errors, entitled[0], filed)
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
    target: Target,
    due: list[BlogPost],
    stored: dict[str, DigestPost],
    summary_errors: dict[str, str],
    filer_sub: DigestSubscription,
    filed: list[str],
) -> str | None:
    """File `due` posts (oldest first) in one project with `filer_sub`'s user's Jira connection;
    returns the error that stopped it, if any."""
    _, project_key = target
    user = await db.get(User, filer_sub.user_id)
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
    jira: ActiveConnection,
    filer: User,
    filer_name: str,
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
    db: AsyncSession, jira: ActiveConnection, filer: User, filer_name: str, target: Target, post: DigestPost
) -> None:
    site_url, project_key = target
    title = f"NHI Blog Digest: {post.title}"
    if len(title) > MAX_TITLE:
        title = title[: MAX_TITLE - 1] + "…"
    filed_by = f"Filed by IdentityHub's NHI Blog Digest with {filer_name}'s Jira connection"
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
            filed_by_user_id=filer.id,
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
