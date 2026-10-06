"""Creating NHI finding tickets in Jira and listing the ones IdentityHub created."""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Finding, FindingSource, User
from app.jira import adf, client
from app.jira.client import JiraTarget
from app.jira.errors import JiraForbidden, JiraNotFound, JiraProjectUnsupported, JiraValidationError
from app.schemas.findings import FindingCreate, FindingCreated, Project, RecentTicket
from app.services.jira_connection import use_jira

# Every ticket IdentityHub creates carries this label; "recent tickets" is a JQL search on it.
APP_LABEL = "identityhub"
RECENT_LIMIT = 10
PROJECT_SEARCH_LIMIT = 50
# Preferred issue types, in order. Projects define their own types, so fall back to any
# non-subtask type when none of these exist.
PREFERRED_ISSUE_TYPES = ("Task", "Bug", "Story")


async def list_projects(db: AsyncSession, user: User, query: str | None) -> list[Project]:
    async with use_jira(db, user) as conn:
        projects = await client.search_projects(conn, query, PROJECT_SEARCH_LIMIT)
    return [Project(id=p["id"], key=p["key"], name=p["name"]) for p in projects]


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
    """The newest tickets IdentityHub created in a project, as visible to this user in Jira.

    Jira is the source of truth, so renamed, moved or deleted issues show up correctly, and tickets
    created through the REST API are included. `project_key` is validated against
    PROJECT_KEY_PATTERN before it reaches here, so it can't break out of the JQL string.
    """
    jql = f'project = "{project_key}" AND labels = "{APP_LABEL}" ORDER BY created DESC'
    async with use_jira(db, user) as conn:
        try:
            issues = await client.search_issues(conn, jql, ["summary", "created"], RECENT_LIMIT)
        except JiraValidationError as exc:
            # JQL rejects unknown projects (or ones the user can't see) with a 400.
            raise JiraNotFound(f"Project {project_key} wasn't found, or your Jira account can't access it.") from exc
    return [
        RecentTicket(
            key=issue["key"],
            summary=issue["fields"]["summary"],
            url=browse_url(conn, issue["key"]),
            created_at=_parse_jira_time(issue["fields"]["created"]),
        )
        for issue in issues
    ]


async def pick_issue_type(conn: JiraTarget, project_key: str) -> str:
    types = [t for t in await client.get_issue_types(conn, project_key) if not t.get("subtask")]
    by_name = {t["name"]: t["id"] for t in types}
    for name in PREFERRED_ISSUE_TYPES:
        if name in by_name:
            return by_name[name]
    if types:
        return types[0]["id"]
    raise JiraProjectUnsupported()


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


def browse_url(conn: JiraTarget, issue_key: str) -> str:
    return f"{conn.site_url.rstrip('/')}/browse/{issue_key}"


def _parse_jira_time(value: str) -> datetime:
    # Jira sends e.g. 2026-10-06T12:34:56.789+0000; Python wants the offset as +00:00.
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
