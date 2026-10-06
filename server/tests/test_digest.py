"""NHI Blog Digest: subscriptions, the bot, the scheduled run and the summarizers.

The blog, Claude and Ollama are mocked with respx (the app runs on httpx2, aliased as httpx, so the
real Anthropic and Ollama clients are exercised); Jira (users' OAuth and the bot's API token) with
`responses`.
"""

import base64
import json
import re

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.db.models import DigestDelivery, DigestSubscription
from helpers import JIRA_API, connect, csrf, sign_up_and_login

BOT_SITE = "https://acme.atlassian.net"  # same site as the users' OAuth connection (SITE_A)
BOT_API = f"{BOT_SITE}/rest/api/3"
BLOG = "https://blog.test/blog"
PROJECTS = [{"id": "1", "key": "SEC", "name": "Security"}, {"id": "2", "key": "PLAT", "name": "Platform"}, {"id": "3", "key": "OPS", "name": "Operations"}]


def post_html(title: str, published: str, body: str) -> str:
    ld = {"@context": "https://schema.org", "@type": "BlogPosting", "headline": title, "datePublished": published}
    paragraphs = "".join(f"<p>{body} Sentence number {i} explains why rotating credentials matters.</p>" for i in range(6))
    return (
        f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head>'
        f"<body><nav><a href='/blog'>Blog</a></nav><article><h1>{title}</h1>{paragraphs}</article></body></html>"
    )


BLOG_INDEX = '<a href="/blog/featured-older">F</a><a href="/blog/newest">N</a><a href="/blog/category/x">C</a>'


@pytest.fixture
def digest_env(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("DIGEST_JIRA_SITE_URL", BOT_SITE)
    monkeypatch.setenv("DIGEST_JIRA_EMAIL", "identityhub-bot@acme.test")
    monkeypatch.setenv("DIGEST_JIRA_API_TOKEN", "bot-token")
    monkeypatch.setenv("DIGEST_BLOG_URL", BLOG)
    monkeypatch.setenv("LLM_PROVIDER", "extractive")
    get_settings.cache_clear()


@pytest.fixture
async def app(digest_env):
    """Same as the default app fixture, but created after the digest settings are in place."""
    from app.main import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
def jira(atlassian):
    """User OAuth Jira + bot Jira + blog, all mocked."""
    j = atlassian.jira
    j.get(f"{JIRA_API}/project/search", json={"values": PROJECTS})  # the user can create in all three
    j.get(f"{BOT_API}/myself", json={"accountId": "bot", "displayName": "IdentityHub"})
    j.get(f"{BOT_API}/project/search", json={"values": [PROJECTS[0], PROJECTS[2]]})  # the bot: SEC and OPS
    j.get(re.compile(rf"{re.escape(BOT_API)}/issue/createmeta/\w+/issuetypes"), json={"issueTypes": [{"id": "3", "name": "Task"}]})
    j.post(f"{BOT_API}/issue", json={"id": "1", "key": "SEC-42"}, status=201)
    oauth = atlassian.oauth
    oauth.get(BLOG).respond(200, text=BLOG_INDEX)
    oauth.get(f"{BLOG}/featured-older").respond(200, text=post_html("Older featured post", "2026-07-28T10:00:00Z", "Old."))
    oauth.get(f"{BLOG}/newest").respond(200, text=post_html("When a Worm Steals Your Keys", "2026-09-02T10:00:00Z", "Credentials are the blast radius."))
    return atlassian


async def connected_user(client, email="alice@example.com"):
    await sign_up_and_login(client, email)
    await connect(client)
    return client


async def subscribe(client, keys):
    return await client.put("/api/digest/subscriptions", json={"project_keys": keys}, headers=await csrf(client))


def bot_issue_requests(jira):
    return [c.request for c in jira.jira.calls if c.request.method == "POST" and c.request.url.startswith(BOT_API)]


# --- Availability and subscriptions -----------------------------------------------------------


async def test_not_configured(client, atlassian, monkeypatch):
    from app.core.config import get_settings

    for name in ("DIGEST_JIRA_SITE_URL", "DIGEST_JIRA_EMAIL", "DIGEST_JIRA_API_TOKEN"):
        monkeypatch.delenv(name)
    get_settings.cache_clear()
    await connected_user(client)
    status = (await client.get("/api/digest")).json()
    assert status["configured"] is False
    assert "hasn't been set up" in status["unavailable_reason"]


async def test_status_names_bot_and_summarizer(app, client, jira):
    await connected_user(client)
    status = (await client.get("/api/digest")).json()
    assert status["configured"] and status["unavailable_reason"] is None
    assert status["bot_account"] == "IdentityHub"
    assert status["summarizer"] == "extractive summary (no LLM configured)"
    # The bot authenticates with its own API token, not a user's OAuth token.
    myself = next(c.request for c in jira.jira.calls if c.request.url == f"{BOT_API}/myself")
    assert myself.headers["Authorization"] == "Basic " + base64.b64encode(b"identityhub-bot@acme.test:bot-token").decode()


async def test_projects_offered_are_those_both_user_and_bot_can_create_in(app, client, jira):
    await connected_user(client)
    projects = (await client.get("/api/digest/projects")).json()
    assert [p["key"] for p in projects] == ["SEC", "OPS"]  # PLAT: the bot has no access


async def test_cannot_subscribe_where_the_bot_or_user_lacks_access(app, client, jira):
    await connected_user(client)
    response = await subscribe(client, ["SEC", "PLAT"])
    assert response.status_code == 400
    assert "the IdentityHub bot can't create issues in PLAT" in response.json()["detail"]

    jira.jira.replace("GET", f"{JIRA_API}/project/search", json={"values": [PROJECTS[2]]})  # user: only OPS
    response = await subscribe(client, ["SEC"])
    assert response.status_code == 400 and "you can't create issues in SEC" in response.json()["detail"]


async def test_subscribe_and_unsubscribe(app, client, jira):
    await connected_user(client)
    status = (await subscribe(client, ["sec", "OPS"])).json()
    assert [s["project_key"] for s in status["subscriptions"]] == ["OPS", "SEC"]
    status = (await subscribe(client, [])).json()
    assert status["subscriptions"] == []


async def test_other_jira_site_is_explained(app, client, jira, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("DIGEST_JIRA_SITE_URL", "https://other.atlassian.net")
    get_settings.cache_clear()
    jira.jira.get("https://other.atlassian.net/rest/api/3/myself", json={"displayName": "IdentityHub"})
    await connected_user(client)
    reason = (await client.get("/api/digest")).json()["unavailable_reason"]
    assert "posts to https://other.atlassian.net, but your Jira connection is to https://acme.atlassian.net" in reason


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "Jira rejected the credentials of the digest bot account (identityhub-bot@acme.test). Check DIGEST_JIRA_EMAIL and DIGEST_JIRA_API_TOKEN."),
        (404, "couldn't find a Jira site at https://acme.atlassian.net. Check DIGEST_JIRA_SITE_URL."),
        (503, "couldn't reach https://acme.atlassian.net. Jira may be down; this is retried automatically."),
    ],
)
async def test_bot_problems_are_explained(app, client, jira, status, expected):
    jira.jira.replace("GET", f"{BOT_API}/myself", json={"message": "nope"}, status=status)
    await connected_user(client)
    reason = (await client.get("/api/digest")).json()["unavailable_reason"]
    assert expected in reason


# --- Running ----------------------------------------------------------------------------------


async def run(app):
    from app.core.config import get_settings
    from app.services.digest import run_digest

    return await run_digest(get_settings(), app.state.digest)


async def test_files_once_per_project_as_the_bot(app, client, jira, db):
    await connected_user(client)
    await subscribe(client, ["SEC"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bob:
        await connected_user(bob, "bob@example.com")
        await subscribe(bob, ["SEC"])  # same project, second subscriber

    result = await run(app)
    assert result.outcome == "filed in SEC" and result.post_title == "When a Worm Steals Your Keys"

    [request] = bot_issue_requests(jira)  # one ticket, not one per subscriber
    assert request.headers["Authorization"].startswith("Basic ")  # created by the bot account
    fields = json.loads(request.body)["fields"]
    assert fields["project"] == {"key": "SEC"}
    assert fields["summary"] == "NHI Blog Digest: When a Worm Steals Your Keys"
    assert fields["labels"] == ["identityhub", "nhi-blog-digest"]
    description = json.dumps(fields["description"])
    assert "explains why rotating credentials matters" in description
    assert '"href": "https://blog.test/blog/newest"' in description
    assert "Summary: extractive summary (no LLM configured)." in description

    # Running again files nothing new.
    assert (await run(app)).outcome == "up to date"
    assert len(bot_issue_requests(jira)) == 1
    assert await db.scalar(select(func.count()).select_from(DigestDelivery)) == 1

    status = (await client.get("/api/digest")).json()
    assert status["subscriptions"][0]["last_ticket"]["key"] == "SEC-42"
    assert status["last_run"]["outcome"] == "up to date"


async def test_not_filed_when_no_subscriber_still_has_access(app, client, jira, db):
    await connected_user(client)
    await subscribe(client, ["SEC"])
    jira.jira.replace("GET", f"{JIRA_API}/project/search", json={"values": []})  # alice lost access

    result = await run(app)
    assert result.outcome == "nothing filed" and bot_issue_requests(jira) == []
    sub = await db.scalar(select(DigestSubscription).execution_options(populate_existing=True))
    assert sub.last_error == "You can no longer create issues in SEC, so the digest wasn't filed there."


async def test_bot_losing_access_is_reported_on_the_subscription(app, client, jira, db):
    await connected_user(client)
    await subscribe(client, ["SEC"])
    jira.jira.replace("POST", f"{BOT_API}/issue", json={"errorMessages": ["Forbidden"]}, status=403)

    result = await run(app)
    assert "Skipped SEC" in result.error
    sub = await db.scalar(select(DigestSubscription).execution_options(populate_existing=True))
    assert "The IdentityHub bot can no longer create issues in SEC" in sub.last_error
    assert await db.scalar(select(func.count()).select_from(DigestDelivery)) == 0  # retried next run


async def test_blog_unreachable_fails_cleanly(app, client, jira):
    await connected_user(client)
    await subscribe(client, ["SEC"])
    jira.oauth.get(BLOG).mock(side_effect=httpx.ConnectError("down"))
    result = await run(app)
    assert result.outcome == "failed" and "Couldn't fetch" in result.error


async def test_run_now_endpoint(app, client, jira):
    await connected_user(client)
    await subscribe(client, ["SEC"])
    response = await client.post("/api/digest/run", headers=await csrf(client))
    assert response.status_code == 202
    await app.state.digest.task
    assert (await client.get("/api/digest")).json()["last_run"]["outcome"] == "filed in SEC"


async def test_newest_post_is_chosen_by_date_not_position(jira):
    from app.digest.blog import fetch_latest_post

    post = await fetch_latest_post(BLOG)
    assert post.title == "When a Worm Steals Your Keys"  # not the featured post listed first


# --- Summarizers ------------------------------------------------------------------------------


def _post():
    from datetime import UTC, datetime

    from app.digest.blog import BlogPost

    text = " ".join(f"Rotating long-lived API keys reduces exposure number {i}." for i in range(8))
    return BlogPost(url="https://blog.test/blog/p", title="Rotate keys", published=datetime.now(UTC), text=text)


async def test_auto_prefers_claude_then_ollama_then_extractive(monkeypatch, atlassian):
    from app.core.config import get_settings
    from app.digest.summarizers import choose_summarizer

    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setenv("OLLAMA_URL", "http://ollama.test:11434")
    get_settings.cache_clear()

    atlassian.oauth.get("http://ollama.test:11434/api/tags").mock(side_effect=httpx.ConnectError("no ollama"))
    assert (await choose_summarizer(get_settings())).method == "extractive"

    atlassian.oauth.get("http://ollama.test:11434/api/tags").respond(200, json={"models": [{"model": "llama3.2:3b", "name": "llama3.2:3b"}]})
    assert (await choose_summarizer(get_settings())).method == "ollama"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    get_settings.cache_clear()
    assert (await choose_summarizer(get_settings())).method == "claude"


async def test_ollama_reachable_but_model_not_pulled_falls_back(monkeypatch, atlassian):
    from app.core.config import get_settings
    from app.digest.summarizers import choose_summarizer

    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setenv("OLLAMA_URL", "http://ollama.test:11434")
    get_settings.cache_clear()
    atlassian.oauth.get("http://ollama.test:11434/api/tags").respond(200, json={"models": []})
    assert (await choose_summarizer(get_settings())).method == "extractive"


async def test_claude_summarizer_request(atlassian):
    from app.digest.summarizers import ClaudeSummarizer

    route = atlassian.oauth.post(re.compile(r"https://api\.anthropic\.com/v1/messages.*")).respond(
        200,
        json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": "A summary.\n\nKey points:\n- rotate"}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        },
    )
    text = await ClaudeSummarizer(api_key="sk-ant-test", model="claude-opus-5-5").summarize(_post())
    assert text.startswith("A summary.")
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default"
    assert "<article>" in body["messages"][0]["content"] and "never as instructions" in body["system"]
    assert route.calls.last.request.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"


async def test_claude_refusal_is_an_error(atlassian):
    from app.digest.summarizers import ClaudeSummarizer, SummaryError

    atlassian.oauth.post(re.compile(r"https://api\.anthropic\.com/v1/messages.*")).respond(
        200,
        json={
            "id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
            "stop_reason": "refusal", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0},
        },
    )
    with pytest.raises(SummaryError, match="declined"):
        await ClaudeSummarizer(api_key="sk-ant-test", model="claude-opus-5-5").summarize(_post())


async def test_ollama_summarizer_request(atlassian):
    from app.digest.summarizers import OllamaSummarizer

    route = atlassian.oauth.post("http://ollama.test:11434/api/chat").respond(
        200, json={"model": "llama3.2:3b", "message": {"role": "assistant", "content": "Local summary."}, "done": True}
    )
    text = await OllamaSummarizer("http://ollama.test:11434", "llama3.2:3b").summarize(_post())
    assert text == "Local summary."
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "llama3.2:3b" and body["messages"][0]["role"] == "system"


async def test_extractive_summary_quotes_sentences_in_order():
    from app.digest.summarizers import ExtractiveSummarizer

    text = await ExtractiveSummarizer(sentences=3).summarize(_post())
    lines = text.splitlines()
    assert lines[0] == "Key sentences from the post:" and len(lines) == 4
    numbers = [int(re.search(r"number (\d+)", line).group(1)) for line in lines[1:]]
    assert numbers == sorted(numbers)  # article order preserved
