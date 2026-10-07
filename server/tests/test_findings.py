"""Projects, finding creation and recent tickets, against a mocked Jira."""

import json
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app.db.models import Finding
from app.jira import adf
from httpx import ASGITransport, AsyncClient

from helpers import JIRA_API, connect, csrf, sign_up_and_login

ISSUE_TYPES = {
    "issueTypes": [
        {"id": "1", "name": "Sub-task", "subtask": True},
        {"id": "2", "name": "Bug", "subtask": False},
        {"id": "3", "name": "Task", "subtask": False},
    ]
}
FINDING = {
    "project_key": "SEC",
    "summary": "Stale Service Account: svc-deploy-prod",
    "description": "Last used 214 days ago.\nOwner left the company.\n\nRotate or delete svc_deploy_prod.",
    "finding_type": "stale_identity",
    "severity": "high",
    "identity_name": "svc-deploy-prod",
}


@pytest.fixture
async def connected(client, atlassian):
    await sign_up_and_login(client, "alice@example.com")
    assert await connect(client) == "http://localhost:8000/settings?jira=connected"
    atlassian.jira.get(f"{JIRA_API}/issue/createmeta/SEC/issuetypes", json=ISSUE_TYPES)
    atlassian.jira.post(f"{JIRA_API}/issue", json={"id": "10001", "key": "SEC-1"}, status=201)
    return client


async def post_finding(client, **overrides):
    return await client.post("/api/findings", json={**FINDING, **overrides}, headers=await csrf(client))


def last_request(atlassian, method: str, path: str):
    return next(c.request for c in reversed(atlassian.jira.calls) if c.request.method == method and path in c.request.url)


# --- Projects ---------------------------------------------------------------------------------


async def test_lists_only_projects_the_user_can_create_issues_in(connected, atlassian):
    atlassian.jira.get(
        f"{JIRA_API}/project/search",
        json={"values": [{"id": "10000", "key": "SEC", "name": "Security", "extra": "ignored"}]},
    )

    response = await connected.get("/api/jira/projects", params={"query": "sec"})
    assert response.json() == [{"id": "10000", "key": "SEC", "name": "Security"}]

    params = parse_qs(urlparse(last_request(atlassian, "GET", "/project/search").url).query)
    assert params["action"] == ["create"] and params["query"] == ["sec"]


async def test_projects_require_a_jira_connection(client):
    await sign_up_and_login(client, "alice@example.com")
    response = await client.get("/api/jira/projects")
    assert response.status_code == 409 and response.json()["code"] == "jira_not_connected"


# --- Creating findings ------------------------------------------------------------------------


async def test_create_finding(connected, atlassian, db):
    response = await post_finding(connected)
    assert response.status_code == 201
    assert response.json() == {
        "key": "SEC-1",
        "url": "https://acme.atlassian.net/browse/SEC-1",
        "summary": "Stale Service Account: svc-deploy-prod",
    }

    fields = json.loads(last_request(atlassian, "POST", "/issue").body)["fields"]
    assert fields["project"] == {"key": "SEC"}
    assert fields["issuetype"] == {"id": "3"}  # Task preferred over Bug; sub-tasks never used
    assert fields["summary"] == "Stale Service Account: svc-deploy-prod"
    assert fields["labels"] == ["identityhub", "nhi-stale-identity", "severity-high"]

    description = json.dumps(fields["description"])
    assert fields["description"]["type"] == "doc"
    assert '"text": "Severity: ", "marks": [{"type": "strong"}]' in description
    assert "svc_deploy_prod" in description  # literal text, never interpreted as markup

    finding = await db.scalar(select(Finding))
    assert (finding.issue_key, finding.source.value, finding.project_key) == ("SEC-1", "ui", "SEC")


async def test_minimal_finding_needs_only_project_and_summary(connected, atlassian):
    response = await connected.post(
        "/api/findings", json={"project_key": "sec", "summary": "Unused API key"}, headers=await csrf(connected)
    )
    assert response.status_code == 201
    fields = json.loads(last_request(atlassian, "POST", "/issue").body)["fields"]
    assert fields["project"] == {"key": "SEC"}  # normalised to upper case
    assert fields["labels"] == ["identityhub"]


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"summary": ""}, "summary"),
        ({"summary": "x" * 256}, "summary"),
        ({"summary": "two\nlines"}, "summary"),
        ({"project_key": "not a key"}, "project_key"),
        ({"severity": "urgent"}, "severity"),
        ({"finding_type": "weird"}, "finding_type"),
        ({"sumary": "typo"}, "sumary"),
    ],
)
async def test_invalid_input_is_rejected_before_calling_jira(connected, atlassian, overrides, field):
    calls_before = len(atlassian.jira.calls)
    response = await post_finding(connected, **overrides)
    assert response.status_code == 422
    assert any(field in error["loc"] for error in response.json()["detail"])
    assert len(atlassian.jira.calls) == calls_before


async def test_falls_back_to_any_standard_issue_type(connected, atlassian):
    atlassian.jira.replace(
        "GET",
        f"{JIRA_API}/issue/createmeta/SEC/issuetypes",
        json={"issueTypes": [{"id": "7", "name": "Sub-task", "subtask": True}, {"id": "8", "name": "Risk"}]},
    )
    assert (await post_finding(connected)).status_code == 201
    assert json.loads(last_request(atlassian, "POST", "/issue").body)["fields"]["issuetype"] == {"id": "8"}


async def test_project_without_usable_issue_type(connected, atlassian):
    atlassian.jira.replace(
        "GET",
        f"{JIRA_API}/issue/createmeta/SEC/issuetypes",
        json={"issueTypes": [{"id": "7", "name": "Sub-task", "subtask": True}]},
    )
    response = await post_finding(connected)
    assert response.status_code == 422 and response.json()["code"] == "jira_project_unsupported"


# --- Jira errors become clear messages --------------------------------------------------------


async def test_no_permission_in_project(connected, atlassian):
    atlassian.jira.replace("POST", f"{JIRA_API}/issue", json={"errorMessages": ["Forbidden"]}, status=403)
    response = await post_finding(connected)
    assert response.status_code == 403
    assert response.json() == {
        "detail": "Your Jira account doesn't have permission to create issues in SEC.",
        "code": "jira_forbidden",
    }


async def test_unknown_project(connected, atlassian):
    atlassian.jira.replace("GET", f"{JIRA_API}/issue/createmeta/SEC/issuetypes", json={"errorMessages": ["No project"]}, status=404)
    response = await post_finding(connected)
    assert response.status_code == 404
    assert response.json()["detail"] == "Project SEC wasn't found, or your Jira account can't access it."


async def test_project_with_required_fields_we_dont_fill(connected, atlassian):
    atlassian.jira.replace(
        "POST", f"{JIRA_API}/issue", json={"errorMessages": [], "errors": {"customfield_10010": "Team is required."}}, status=400
    )
    response = await post_finding(connected)
    assert response.status_code == 422
    assert response.json() == {"detail": "Jira rejected the request: Team is required.", "code": "jira_validation"}


async def test_jira_rejecting_the_token_marks_connection_for_reconnect(connected, atlassian, db):
    atlassian.jira.replace("POST", f"{JIRA_API}/issue", json={"message": "Unauthorized"}, status=401)
    response = await post_finding(connected)
    assert response.status_code == 409 and response.json()["code"] == "jira_reauth_required"
    assert (await connected.get("/api/jira/connection")).json()["status"] == "needs_reauth"
    assert await db.scalar(select(Finding)) is None


@pytest.mark.parametrize(("jira_status", "status", "code"), [(429, 429, "jira_rate_limited"), (503, 502, "jira_unavailable")])
async def test_jira_overloaded_or_down(connected, atlassian, jira_status, status, code):
    atlassian.jira.replace("POST", f"{JIRA_API}/issue", json={"errorMessages": ["busy"]}, status=jira_status)
    response = await post_finding(connected)
    assert response.status_code == status and response.json()["code"] == code


async def test_create_requires_login(client):
    response = await client.post("/api/findings", json=FINDING, headers=await csrf(client))
    assert response.status_code == 401


# --- Recent tickets ---------------------------------------------------------------------------


async def test_recent_tickets(connected, atlassian):
    atlassian.jira.get(
        f"{JIRA_API}/search/jql",
        json={
            "issues": [
                {"key": "SEC-2", "fields": {"summary": "Exposed key", "created": "2026-10-06T12:34:56.789+0000"}},
                {"key": "SEC-1", "fields": {"summary": "Stale account", "created": "2026-10-05T08:00:00.000+0300"}},
            ]
        },
    )

    response = await connected.get("/api/findings/recent", params={"project_key": "SEC"})
    assert response.status_code == 200
    assert response.json() == [
        {"key": "SEC-2", "summary": "Exposed key", "url": "https://acme.atlassian.net/browse/SEC-2", "created_at": "2026-10-06T12:34:56.789000Z", "deleted": False},
        {"key": "SEC-1", "summary": "Stale account", "url": "https://acme.atlassian.net/browse/SEC-1", "created_at": "2026-10-05T08:00:00+03:00", "deleted": False},
    ]

    params = parse_qs(urlparse(last_request(atlassian, "GET", "/search/jql").url).query)
    assert params["jql"] == ['project = "SEC" AND labels = "identityhub" ORDER BY created DESC']
    assert params["maxResults"] == ["10"]
    assert params["fields"] == ["summary,created"]


def jira_search_returns(atlassian, issues):
    atlassian.jira.upsert("GET", f"{JIRA_API}/search/jql", json={"issues": issues})


def bulk_fetch_returns(atlassian, issues, missing=()):
    atlassian.jira.post(
        f"{JIRA_API}/issue/bulkfetch",
        json={"issues": issues, "issueErrors": [{"key": k, "errorMessages": ["Issue does not exist"]} for k in missing]},
    )


async def test_own_deleted_ticket_is_flagged_not_dropped(connected, atlassian):
    assert (await post_finding(connected)).status_code == 201  # SEC-1, recorded locally
    jira_search_returns(atlassian, [])  # ...then deleted in Jira
    bulk_fetch_returns(atlassian, [], missing=["SEC-1"])

    [ticket] = (await connected.get("/api/findings/recent", params={"project_key": "SEC"})).json()
    assert ticket["key"] == "SEC-1" and ticket["deleted"] is True and ticket["url"] is None
    assert ticket["summary"] == "Stale Service Account: svc-deploy-prod"  # from our own record

    body = json.loads(last_request(atlassian, "POST", "/issue/bulkfetch").body)
    assert body["issueIdsOrKeys"] == ["SEC-1"]


async def test_own_ticket_not_yet_in_search_index_is_shown_normally(connected, atlassian):
    await post_finding(connected)
    jira_search_returns(atlassian, [])  # search index hasn't caught up
    bulk_fetch_returns(atlassian, [{"key": "SEC-1", "fields": {"summary": "Renamed in Jira", "created": "2026-10-06T12:34:56.789+0000"}}])

    [ticket] = (await connected.get("/api/findings/recent", params={"project_key": "SEC"})).json()
    assert ticket["deleted"] is False and ticket["summary"] == "Renamed in Jira"
    assert ticket["url"] == "https://acme.atlassian.net/browse/SEC-1"


async def test_other_users_deleted_tickets_are_not_shown(connected, atlassian, app):
    await post_finding(connected)  # alice's ticket
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bob:
        await sign_up_and_login(bob, "bob@example.com")
        await connect(bob)
        jira_search_returns(atlassian, [])
        response = await bob.get("/api/findings/recent", params={"project_key": "SEC"})
    assert response.json() == []
    assert not any("/issue/bulkfetch" in c.request.url for c in atlassian.jira.calls)  # nothing of bob's to check


async def test_recent_tickets_for_unknown_project(connected, atlassian):
    atlassian.jira.get(
        f"{JIRA_API}/search/jql",
        json={"errorMessages": ["The value 'NOPE' does not exist for the field 'project'."]},
        status=400,
    )
    response = await connected.get("/api/findings/recent", params={"project_key": "NOPE"})
    assert response.status_code == 404 and "NOPE" in response.json()["detail"]


async def test_recent_tickets_rejects_jql_injection(connected, atlassian):
    calls_before = len(atlassian.jira.calls)
    response = await connected.get("/api/findings/recent", params={"project_key": 'SEC" OR project = "HR'})
    assert response.status_code == 422
    assert len(atlassian.jira.calls) == calls_before


# --- ADF --------------------------------------------------------------------------------------


def test_plain_text_to_adf():
    assert adf.plain_paragraphs("one\ntwo\n\n\nthree\n") == [
        adf.paragraph(adf.text("one"), {"type": "hardBreak"}, adf.text("two")),
        adf.paragraph(adf.text("three")),
    ]
    assert adf.plain_paragraphs("   ") == []
