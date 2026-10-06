"""Jira Cloud REST calls on behalf of a user, via atlassian-python-api.

The library is synchronous (requests), so calls run in FastAPI's threadpool to avoid blocking the
event loop. Every call uses the *user's own* OAuth token, so Jira enforces that user's
permissions.
"""

from collections.abc import Callable
from typing import Any, TypeVar

from atlassian import JiraCloud
from requests import HTTPError, RequestException
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.jira.errors import JiraError, JiraReauthRequired, JiraUnavailable
from app.jira.oauth import API_BASE_URL

T = TypeVar("T")


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
    """Run a blocking Jira call in the threadpool and translate transport/auth failures."""
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 401:
            raise JiraReauthRequired() from exc
        raise JiraError() from exc
    except RequestException as exc:
        raise JiraUnavailable() from exc


async def get_myself(cloud_id: str, access_token: str) -> dict[str, Any]:
    client = build_client(cloud_id, access_token)
    return await call(client.get_current_user)
