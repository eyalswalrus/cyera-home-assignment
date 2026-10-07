"""Creating NHI finding tickets in Jira and listing the ones IdentityHub created."""

import uuid
from datetime import datetime

from cachetools import TTLCache
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Finding, FindingSource, User
from app.jira import adf, client
from app.jira.client import ActiveConnection
from app.jira.errors import JiraForbidden, JiraNotFound, JiraProjectUnsupported, JiraValidationError
from app.schemas.findings import FindingCreate, FindingCreated, Project, RecentTicket
from app.services.jira_connection import use_jira

# Every ticket IdentityHub creates carries this label; "recent tickets" is a JQL search on it.
APP_LABEL = "identityhub"
RECENT_LIMIT = 10
# The picker loads one page and lets Jira search the rest. Jira's rate limit charges a point per
# project returned, so a smaller first page is cheaper on every page view.
PROJECT_SEARCH_LIMIT = 20
# Preferred issue types, in order. Projects define their own types, so fall back to any
# non-subtask type when none of these exist.
PREFERRED_ISSUE_TYPES = ("Task", "Bug", "Story")

# Rate-limit savings (see app/jira/client.py). In-process caches; several replicas would each keep
# their own, which is fine for data this stable.
# * Issue types per (Jira account, project) for an hour: creating a ticket then costs 1 point, not
#   ~7. Per account, not per site: the lookup is also the check that the account can create issues
#   there (giving a clear "no permission" error), so one user's lookup mustn't stand in for another's.
#   Dropped when Jira rejects a create, so a changed project configuration is picked up.
# * The project picker per user for 5 minutes. Permission checks (API keys, digest) never use it.
_issue_types: TTLCache[tuple[str, str], str] = TTLCache(maxsize=2048, ttl=3600)
_project_lists: TTLCache[tuple[uuid.UUID, str, str], list[Project]] = TTLCache(maxsize=2048, ttl=300)


def clear_caches() -> None:
    _issue_types.clear()
    _project_lists.clear()


async def list_projects(db: AsyncSession, user: User, query: str | None) -> list[Project]:
    async with use_jira(db, user) as conn:
        cache_key = (user.id, conn.cloud_id, (query or "").strip().lower())
        if (cached := _project_lists.get(cache_key)) is not None:
            return cached
        projects = await client.search_projects(conn, query, PROJECT_SEARCH_LIMIT)
    result = [Project(id=p["id"], key=p["key"], name=p["name"]) for p in projects]
    _project_lists[cache_key] = result
    return result


async def create_finding(
    db: AsyncSession,
    user: User,
    finding: FindingCreate,
    source: FindingSource,
    api_key_id: uuid.UUID | None = None,
) -> FindingCreated:
    key = finding.project_key
    async with use_jira(db, user) as conn:
        try:
            issue_type_id = await pick_issue_type(conn, key)
            issue = await client.create_issue(
                conn,
                {
                    "project": {"key": key},
                    "issuetype": {"id": issue_type_id},
                    "summary": finding.summary,
                    "description": _description(finding),
                    "labels": _labels(finding),
                },
            )
        except JiraValidationError:
            forget_issue_type(conn, key)  # the cached type may be stale; refetch next time
            raise
        except JiraForbidden as exc:
            raise JiraForbidden(f"Your Jira account doesn't have permission to create issues in {key}.") from exc
        except JiraNotFound as exc:
            raise JiraNotFound(f"Project {key} wasn't found, or your Jira account can't access it.") from exc

    created = FindingCreated(key=issue["key"], url=browse_url(conn, issue["key"]), summary=finding.summary)
    db.add(
        Finding(
            user_id=user.id,
            source=source,
            api_key_id=api_key_id,
            cloud_id=conn.cloud_id,
            project_key=key,
            issue_key=created.key,
            issue_url=created.url,
            summary=finding.summary,
        )
    )
    await db.commit()
    return created


async def recent_findings(db: AsyncSession, user: User, project_key: str) -> list[RecentTicket]:
    """The newest tickets IdentityHub created in a project.

    Jira is the source of truth, so renamed issues show their current title and tickets created
    through the REST API or by teammates are included. On top of that, the user's *own* tickets that
    Jira no longer returns are kept in the list, flagged as deleted, so a finding can't silently
    disappear. Other users' records are never used here (they belong to their own tenant).

    `project_key` is validated against PROJECT_KEY_PATTERN before it reaches here, so it can't break
    out of the JQL string.
    """
    jql = f'project = "{project_key}" AND labels = "{APP_LABEL}" ORDER BY created DESC'
    async with use_jira(db, user) as conn:
        try:
            issues = await client.search_issues(conn, jql, ["summary", "created"], RECENT_LIMIT)
        except JiraValidationError as exc:
            # JQL rejects unknown projects (or ones the user can't see) with a 400.
            raise JiraNotFound(f"Project {project_key} wasn't found, or your Jira account can't access it.") from exc

        # The user's own recent tickets in this project that the search didn't return: either not in
        # Jira's search index yet (just created), or gone (deleted, moved, access lost).
        found_keys = {issue["key"] for issue in issues}
        mine = (
            await db.scalars(
                select(Finding)
                .where(Finding.user_id == user.id, Finding.cloud_id == conn.cloud_id, Finding.project_key == project_key)
                .order_by(Finding.created_at.desc())
                .limit(RECENT_LIMIT)
            )
        ).all()
        unmatched = [f for f in mine if f.issue_key not in found_keys]
        still_there = await client.fetch_issues(conn, [f.issue_key for f in unmatched], ["summary", "created"])

    tickets = {
        issue["key"]: RecentTicket(
            key=issue["key"],
            summary=issue["fields"]["summary"],
            url=browse_url(conn, issue["key"]),
            created_at=_parse_jira_time(issue["fields"]["created"]),
        )
        for issue in [*issues, *still_there]
    }
    for finding in unmatched:
        if finding.issue_key not in tickets:
            tickets[finding.issue_key] = RecentTicket(
                key=finding.issue_key, summary=finding.summary, url=None, created_at=finding.created_at, deleted=True
            )
    return sorted(tickets.values(), key=lambda t: t.created_at, reverse=True)[:RECENT_LIMIT]


async def pick_issue_type(conn: ActiveConnection, project_key: str) -> str:
    cache_key = (conn.identity, project_key)
    if (cached := _issue_types.get(cache_key)) is not None:
        return cached
    types = [t for t in await client.get_issue_types(conn, project_key) if not t.get("subtask")]
    by_name = {t["name"]: t["id"] for t in types}
    chosen = next((by_name[name] for name in PREFERRED_ISSUE_TYPES if name in by_name), None)
    if chosen is None and types:
        chosen = types[0]["id"]
    if chosen is None:
        raise JiraProjectUnsupported()
    _issue_types[cache_key] = chosen
    return chosen


def forget_issue_type(conn: ActiveConnection, project_key: str) -> None:
    _issue_types.pop((conn.identity, project_key), None)


def _description(finding: FindingCreate) -> adf.Node:
    details = [
        ("Finding type", finding.finding_type.label if finding.finding_type else None),
        ("Severity", finding.severity.value.capitalize() if finding.severity else None),
        ("Identity", finding.identity_name),
    ]
    content = [adf.paragraph(adf.text(f"{label}: ", "strong"), adf.text(value)) for label, value in details if value]
    content += adf.plain_paragraphs(finding.description)
    content.append(adf.paragraph(adf.text("Reported via IdentityHub", "em")))
    return adf.document(*content)


def _labels(finding: FindingCreate) -> list[str]:
    labels = [APP_LABEL]
    if finding.finding_type:
        labels.append(f"nhi-{finding.finding_type.value.replace('_', '-')}")
    if finding.severity:
        labels.append(f"severity-{finding.severity.value}")
    return labels


def browse_url(conn: ActiveConnection, issue_key: str) -> str:
    return f"{conn.site_url.rstrip('/')}/browse/{issue_key}"


def _parse_jira_time(value: str) -> datetime:
    # Jira sends e.g. 2026-10-06T12:34:56.789+0000; Python wants the offset as +00:00.
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
