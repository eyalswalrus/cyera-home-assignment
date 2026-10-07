"""Staying within Jira's rate limits: caching, burst retries, the hourly quota guard, digest jitter."""

import random
from datetime import UTC, datetime

from app.jira.client import BotConnection, quota_guard, search_projects
from app.services.digest import planned_run_at

from helpers import JIRA_API
from test_findings import ISSUE_TYPES, connected, post_finding  # noqa: F401  (fixture)

PROJECTS = {"values": [{"id": "10000", "key": "SEC", "name": "Security"}]}


def calls_to(atlassian, method: str, path: str) -> int:
    return sum(1 for c in atlassian.jira.calls if c.request.method == method and path in c.request.url)


# --- Caching ----------------------------------------------------------------------------------


async def test_issue_types_are_looked_up_once_per_project(connected, atlassian):
    for _ in range(2):
        assert (await post_finding(connected)).status_code == 201
    assert calls_to(atlassian, "GET", "/createmeta/SEC/issuetypes") == 1


async def test_issue_type_cache_is_dropped_when_jira_rejects_a_create(connected, atlassian):
    assert (await post_finding(connected)).status_code == 201
    atlassian.jira.replace("POST", f"{JIRA_API}/issue", json={"errors": {"issuetype": "invalid"}}, status=400)
    assert (await post_finding(connected)).status_code == 422
    atlassian.jira.replace("POST", f"{JIRA_API}/issue", json={"id": "10002", "key": "SEC-2"}, status=201)
    assert (await post_finding(connected)).status_code == 201
    assert calls_to(atlassian, "GET", "/createmeta/SEC/issuetypes") == 2


async def test_project_list_is_cached_per_query(connected, atlassian):
    atlassian.jira.get(f"{JIRA_API}/project/search", json=PROJECTS)
    for query in ("sec", "sec", "ops"):
        assert (await connected.get("/api/jira/projects", params={"query": query})).status_code == 200
    assert calls_to(atlassian, "GET", "/project/search") == 2


# --- Burst limits -----------------------------------------------------------------------------


async def test_burst_limit_is_retried(connected, atlassian):
    busy = {"status": 429, "headers": {"Retry-After": "1", "RateLimit-Reason": "jira-burst-based"}}
    atlassian.jira.replace("POST", f"{JIRA_API}/issue", **busy)
    atlassian.jira.add("POST", f"{JIRA_API}/issue", json={"id": "10001", "key": "SEC-1"}, status=201)
    # `responses` serves registrations for the same URL in order: one 429, then success.
    response = await post_finding(connected)
    assert response.status_code == 201
    assert calls_to(atlassian, "POST", "/issue") == 2


async def test_persistent_burst_limit_is_reported_with_retry_after(connected, atlassian):
    atlassian.jira.replace(
        "POST", f"{JIRA_API}/issue", status=429, headers={"Retry-After": "2", "RateLimit-Reason": "jira-burst-based"}
    )
    response = await post_finding(connected)
    assert response.status_code == 429 and response.json()["code"] == "jira_rate_limited"
    assert response.headers["Retry-After"] == "2"
    assert calls_to(atlassian, "POST", "/issue") == 4  # first try + 3 retries


async def test_long_retry_after_is_not_waited_out(connected, atlassian):
    atlassian.jira.replace(
        "POST", f"{JIRA_API}/issue", status=429, headers={"Retry-After": "30", "RateLimit-Reason": "jira-burst-based"}
    )
    response = await post_finding(connected)
    assert response.status_code == 429 and response.headers["Retry-After"] == "30"
    assert calls_to(atlassian, "POST", "/issue") == 1


# --- Hourly quota -----------------------------------------------------------------------------


async def test_exhausted_global_quota_pauses_all_jira_calls(connected, atlassian):
    atlassian.jira.replace(
        "POST", f"{JIRA_API}/issue", status=429, headers={"Retry-After": "1800", "RateLimit-Reason": "jira-quota-global-based"}
    )
    response = await post_finding(connected)
    assert response.status_code == 429
    assert response.json()["detail"] == (
        "IdentityHub has used its hourly Jira API quota. It resets in about 30 minutes; please try again then."
    )
    assert calls_to(atlassian, "POST", "/issue") == 1

    # Every later call is refused without touching Jira, for any site.
    before = len(atlassian.jira.calls)
    assert (await connected.get("/api/jira/projects")).status_code == 429
    assert len(atlassian.jira.calls) == before
    other_site = BotConnection("https://globex.atlassian.net", "bot@example.com", "token")
    try:
        await search_projects(other_site, None, 10)
        raise AssertionError("expected the quota guard to refuse")
    except Exception as exc:
        assert type(exc).__name__ == "JiraRateLimited"


async def test_exhausted_tenant_quota_pauses_only_that_site(connected, atlassian):
    atlassian.jira.replace(
        "POST", f"{JIRA_API}/issue", status=429, headers={"Retry-After": "600", "RateLimit-Reason": "jira-quota-tenant-based"}
    )
    assert (await post_finding(connected)).status_code == 429
    assert (await connected.get("/api/jira/projects")).status_code == 429

    atlassian.jira.get("https://globex.atlassian.net/rest/api/3/project/search", json=PROJECTS)
    other_site = BotConnection("https://globex.atlassian.net", "bot@example.com", "token")
    assert await search_projects(other_site, None, 10) == PROJECTS["values"]
    quota_guard.clear()


# --- Digest schedule --------------------------------------------------------------------------


def test_digest_runs_at_a_random_point_within_the_jitter_window():
    now = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)
    runs = [planned_run_at("09:00", 30, now, random.Random(seed)) for seed in range(50)]
    start = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
    assert all(start <= run <= start.replace(minute=30) for run in runs)
    assert len(set(runs)) > 1
    assert planned_run_at("09:00", 0, now) == start
