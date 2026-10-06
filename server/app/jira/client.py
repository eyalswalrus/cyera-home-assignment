"""Jira Cloud REST calls on behalf of a user, via atlassian-python-api.

The library is synchronous (requests), so calls run in FastAPI's threadpool to avoid blocking the
event loop. Every call uses the *user's own* OAuth token, so Jira enforces that user's
permissions.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from atlassian import JiraCloud
from requests import HTTPError, RequestException
from starlette.concurrency import run_in_threadpool

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

T = TypeVar("T")


@dataclass(frozen=True)
class ActiveConnection:
    """What's needed to call Jira as a user: their site and a currently valid access token."""

    cloud_id: str
    site_url: str
    access_token: str


def build_client(cloud_id: str, access_token: str) -> JiraCloud:
    return JiraCloud(
        url=API_BASE_URL.format(cloud_id=cloud_id),
        oauth2={
            "client_id": get_settings().atlassian_client_id,
            "token": {"access_token": access_token, "token_type": "Bearer"},
        },
        timeout=15,
    )


async def call(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a blocking Jira call in the threadpool and translate failures into JiraErrors.

    Callers can catch the specific types to add context (e.g. which project was forbidden).
    """
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
            raise JiraRateLimited() from exc
        if status is not None and status >= 500:
            raise JiraUnavailable() from exc
        raise JiraError() from exc
    except RequestException as exc:
        raise JiraUnavailable() from exc


async def get_myself(cloud_id: str, access_token: str) -> dict[str, Any]:
    client = build_client(cloud_id, access_token)
    return await call(client.get_current_user)


async def search_projects(conn: ActiveConnection, query: str | None, limit: int) -> list[dict[str, Any]]:
    """Projects the user may *create issues in* (not merely browse)."""
    client = build_client(conn.cloud_id, conn.access_token)
    page = await call(client.search_projects, action="create", query=query or None, max_results=limit, order_by="name")
    return page.get("values", [])


async def get_issue_types(conn: ActiveConnection, project_key: str) -> list[dict[str, Any]]:
    """Issue types the user can create in the project."""
    client = build_client(conn.cloud_id, conn.access_token)
    page = await call(client.get_create_issue_meta_issue_types, project_key)
    # Current API returns `issueTypes`; some deployments still answer with `values`.
    return page.get("issueTypes", page.get("values", []))


async def create_issue(conn: ActiveConnection, fields: dict[str, Any]) -> dict[str, Any]:
    client = build_client(conn.cloud_id, conn.access_token)
    return await call(client.create_issue, data={"fields": fields})


async def search_issues(conn: ActiveConnection, jql: str, fields: list[str], limit: int) -> list[dict[str, Any]]:
    client = build_client(conn.cloud_id, conn.access_token)
    result = await call(client.enhanced_jql, jql, fields=fields, limit=limit)
    return result.get("issues", [])
