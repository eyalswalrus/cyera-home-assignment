"""Jira Cloud REST calls on behalf of a user, via atlassian-python-api.

The library is synchronous (requests), so calls run in FastAPI's threadpool to avoid blocking the
event loop. Every call uses the *user's own* OAuth token, so Jira enforces that user's
permissions.

Rate limits (https://developer.atlassian.com/cloud/jira/platform/rate-limiting/): Jira answers
429 with a `RateLimit-Reason`, and each reason needs a different response:

* **Burst** (`jira-burst-based`, `jira-per-issue-on-write`): too many requests this second. Retried
  here a few times, waiting `Retry-After` plus jitter, when that wait is short.
* **Hourly points quota** (`jira-quota-global-based`, `jira-quota-tenant-based`): retrying can't help
  until the quota resets. The quota guard stops *all* calls to that pool until then (every customer
  for the global pool, one site for a per-tenant pool) and tells the caller when to come back,
  rather than hammering Jira and tying up threads.
"""

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import urlparse

from atlassian import JiraCloud
from requests import HTTPError, RequestException, Response
from starlette.concurrency import run_in_threadpool
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, stop_after_attempt

from app.core.config import get_settings
from app.jira.errors import (
    JiraError,
    JiraForbidden,
    JiraNotFound,
    JiraRateLimited,
    JiraReauthRequired,
    JiraUnavailable,
    JiraValidationError,
)
from app.jira.oauth import API_BASE_URL

log = logging.getLogger(__name__)
T = TypeVar("T")

BURST_RETRIES = 3
MAX_INLINE_WAIT_SECONDS = 5  # longer Retry-After values are surfaced, not waited out in a request
QUOTA_REASONS = {"jira-quota-global-based", "jira-quota-tenant-based"}
# The library's own 429 handling sleeps for Retry-After (up to minutes) inside a worker thread;
# turn it off so the policy below is the only one.
CLIENT_OPTIONS: dict[str, Any] = {"timeout": 15, "retry_with_header": False, "backoff_and_retry": False}


@dataclass(frozen=True)
class ActiveConnection:
    """What's needed to call Jira as a user: their site and a currently valid access token."""

    cloud_id: str
    site_url: str
    access_token: str
    account_id: str | None = None

    @property
    def identity(self) -> str:
        """Who Jira sees: per-account caches are keyed by this."""
        return f"{self.cloud_id}:{self.account_id or self.access_token}"

    def client(self) -> JiraCloud:
        return _watched(build_client(self.cloud_id, self.access_token))


def build_client(cloud_id: str, access_token: str) -> JiraCloud:
    return JiraCloud(
        url=API_BASE_URL.format(cloud_id=cloud_id),
        oauth2={
            "client_id": get_settings().atlassian_client_id,
            "token": {"access_token": access_token, "token_type": "Bearer"},
        },
        **CLIENT_OPTIONS,
    )


# --- Rate limits ------------------------------------------------------------------------------


class QuotaGuard:
    """Remembers until when an exhausted hourly quota blocks calls: "global" for the app-wide pool
    (shared by all customers), or a site for a per-tenant pool. In-process; with several replicas
    this state would live in a shared store such as Redis."""

    def __init__(self) -> None:
        self._blocked_until: dict[str, float] = {}

    def check(self, site: str) -> None:
        now = time.time()
        for scope in ("global", site):
            until = self._blocked_until.get(scope, 0)
            if until > now:
                raise _quota_exhausted(until - now)

    def block(self, scope: str, seconds: float) -> None:
        self._blocked_until[scope] = max(self._blocked_until.get(scope, 0), time.time() + seconds)

    def clear(self) -> None:
        self._blocked_until.clear()


quota_guard = QuotaGuard()


def _quota_exhausted(seconds: float) -> JiraRateLimited:
    minutes = max(1, round(seconds / 60))
    return JiraRateLimited(
        f"IdentityHub has used its hourly Jira API quota. It resets in about {minutes} "
        f"minute{'s' * (minutes != 1)}; please try again then.",
        headers={"Retry-After": str(max(1, int(seconds)))},
    )


class _BurstLimited(Exception):
    """Internal: a short per-second limit; retried with backoff."""

    def __init__(self, retry_after: float, error: JiraRateLimited) -> None:
        self.retry_after = retry_after
        self.error = error


def _wait_retry_after(retry_state: RetryCallState) -> float:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    retry_after = exc.retry_after if isinstance(exc, _BurstLimited) else 1.0
    # Atlassian's guidance: honour Retry-After, plus random jitter so retries don't line up.
    return retry_after * random.uniform(1.0, 1.3) + random.uniform(0, 0.25)


def _site_key(url: str) -> str:
    return urlparse(url).netloc.lower()


_last_near_limit_warning = 0.0


def _watched(client: JiraCloud) -> JiraCloud:
    """Log (at most once a minute) when Jira says we're close to a rate limit."""

    def hook(response: Response, *args: Any, **kwargs: Any) -> None:
        global _last_near_limit_warning
        near = response.headers.get("X-RateLimit-NearLimit") == "true" or ";r=" in response.headers.get(
            "RateLimit", ""
        )
        if near and time.time() - _last_near_limit_warning > 60:
            _last_near_limit_warning = time.time()
            log.warning(
                "Jira rate limit nearly reached: RateLimit=%s RateLimit-Policy=%s",
                response.headers.get("RateLimit") or response.headers.get("Beta-RateLimit"),
                response.headers.get("RateLimit-Policy") or response.headers.get("Beta-RateLimit-Policy"),
            )

    client.session.hooks["response"].append(hook)
    return client


# --- Calling Jira -----------------------------------------------------------------------------


async def call(conn: ActiveConnection, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a blocking Jira call in the threadpool, applying the rate-limit policy above, and
    translate failures into JiraErrors. Callers can catch the specific types to add context."""
    site = _site_key(conn.site_url)
    quota_guard.check(site)
    try:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(_BurstLimited),
            wait=_wait_retry_after,
            stop=stop_after_attempt(BURST_RETRIES + 1),
            reraise=True,
        ):
            with attempt:
                return await _call_once(site, fn, *args, **kwargs)
    except _BurstLimited as exc:
        raise exc.error from None
    raise AssertionError("unreachable")  # pragma: no cover


async def _call_once(site: str, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        # atlassian-python-api puts Jira's own error messages in the exception text.
        jira_says = "; ".join(line for line in str(exc).splitlines() if line.strip())
        if status == 400:
            raise JiraValidationError(f"Jira rejected the request: {jira_says}") from exc
        if status == 401:
            raise JiraReauthRequired() from exc
        if status == 403:
            raise JiraForbidden() from exc
        if status == 404:
            raise JiraNotFound() from exc
        if status == 429:
            raise _rate_limited(site, exc.response) from exc
        if status is not None and status >= 500:
            raise JiraUnavailable() from exc
        raise JiraError() from exc
    except RequestException as exc:
        raise JiraUnavailable() from exc


def _rate_limited(site: str, response: Response | None) -> Exception:
    headers = response.headers if response is not None else {}
    reason = headers.get("RateLimit-Reason", "")
    try:
        retry_after = float(headers.get("Retry-After", "1"))
    except ValueError:
        retry_after = 1.0
    if reason in QUOTA_REASONS:
        scope = "global" if reason == "jira-quota-global-based" else site
        quota_guard.block(scope, retry_after)
        log.warning("Jira hourly quota exhausted (%s, scope %s); pausing Jira calls for %ss", reason, scope, retry_after)
        return _quota_exhausted(retry_after)
    error = JiraRateLimited(headers={"Retry-After": str(max(1, int(retry_after)))})
    if retry_after <= MAX_INLINE_WAIT_SECONDS:
        return _BurstLimited(retry_after, error)
    return error


async def get_account(conn: ActiveConnection) -> dict[str, Any]:
    return await call(conn, conn.client().get_current_user)


async def search_projects(
    conn: ActiveConnection, query: str | None, limit: int, keys: list[str] | None = None
) -> list[dict[str, Any]]:
    """Projects the account may *create issues in* (not merely browse), optionally only `keys`."""
    client = conn.client()
    page = await call(
        conn,
        client.search_projects,
        action="create",
        query=query or None,
        keys=keys or None,
        max_results=limit,
        order_by="name",
    )
    return page.get("values", [])


async def get_issue_types(conn: ActiveConnection, project_key: str) -> list[dict[str, Any]]:
    """Issue types the account can create in the project."""
    client = conn.client()
    page = await call(conn, client.get_create_issue_meta_issue_types, project_key)
    # Current API returns `issueTypes`; some deployments still answer with `values`.
    return page.get("issueTypes", page.get("values", []))


async def create_issue(conn: ActiveConnection, fields: dict[str, Any]) -> dict[str, Any]:
    client = conn.client()
    return await call(conn, client.create_issue, data={"fields": fields})


async def search_issues(conn: ActiveConnection, jql: str, fields: list[str], limit: int) -> list[dict[str, Any]]:
    client = conn.client()
    result = await call(conn, client.enhanced_jql, jql, fields=fields, limit=limit)
    return result.get("issues", [])


async def fetch_issues(conn: ActiveConnection, keys: list[str], fields: list[str]) -> list[dict[str, Any]]:
    """The issues among `keys` that exist and are visible. Unlike a `key in (...)` JQL query, which
    fails entirely if any key is unknown, bulk fetch reports missing issues individually."""
    if not keys:
        return []
    result = await call(conn, conn.client().bulk_fetch_issues, data={"issueIdsOrKeys": keys, "fields": fields})
    return result.get("issues", [])
