"""NHI Blog Digest: subscriptions, the bot, the scheduled run and the summarizers.

The blog, Claude and Ollama are mocked with respx (the app runs on httpx2, aliased as httpx, so the
real Anthropic and Ollama clients are exercised); Jira (users' OAuth and the bot's API token) with
`responses`.
"""

import base64
import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from app.db.models import DigestDelivery, DigestPost, DigestSubscription
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
    oauth.get(f"{BLOG}/featured-older").respond(200, text=post_html("Older featured post", days_ago(20), "Old."))
    oauth.get(f"{BLOG}/newest").respond(200, text=post_html("When a Worm Steals Your Keys", days_ago(2), "Credentials are the blast radius."))
    return atlassian


def days_ago(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def publish_new_post(jira, title="Brand new post", age_days=0.5):
    """Adds a newer post to the mocked blog index."""
    jira.oauth.get(BLOG).respond(200, text=BLOG_INDEX + '<a href="/blog/brand-new">B</a>')
    jira.oauth.get(f"{BLOG}/brand-new").respond(200, text=post_html(title, days_ago(age_days), "Fresh news."))


async def backdate_subscriptions(db, days: float) -> None:
    """Pretend the subscriptions were made `days` ago (fresh start counts from subscribing)."""
    await db.execute(update(DigestSubscription).values(created_at=datetime.now(UTC) - timedelta(days=days)))
    await db.commit()


async def connected_user(client, email="alice@example.com"):
    await sign_up_and_login(client, email)
    await connect(client)
    return client


async def subscribe(client, keys):
    return await client.put("/api/digest/subscriptions", json={"project_keys": keys}, headers=await csrf(client))


def bot_issue_requests(jira):
    return [c.request for c in jira.jira.calls if c.request.method == "POST" and c.request.url.startswith(BOT_API)]


# --- Availability and subscriptions -----------------------------------------------------------


async def test_not_configured_without_jira(client, atlassian, monkeypatch):
    from app.core.config import get_settings

    await connected_user(client)
    for name in ("ATLASSIAN_CLIENT_ID", "ATLASSIAN_CLIENT_SECRET", "DIGEST_JIRA_SITE_URL", "DIGEST_JIRA_EMAIL", "DIGEST_JIRA_API_TOKEN"):
        monkeypatch.delenv(name)
    get_settings.cache_clear()
    status = (await client.get("/api/digest")).json()
    assert status["configured"] is False
    assert "Jira integration isn't configured" in status["unavailable_reason"]


# --- Without a bot account: filed with a subscriber's own Jira connection ----------------------


@pytest.fixture
def no_bot(monkeypatch):
    from app.core.config import get_settings

    for name in ("DIGEST_JIRA_SITE_URL", "DIGEST_JIRA_EMAIL", "DIGEST_JIRA_API_TOKEN"):
        monkeypatch.delenv(name)
    get_settings.cache_clear()


@pytest.fixture
def user_jira_writes(jira):
    jira.jira.get(re.compile(rf"{re.escape(JIRA_API)}/issue/createmeta/\w+/issuetypes"), json={"issueTypes": [{"id": "3", "name": "Task"}]})
    jira.jira.post(f"{JIRA_API}/issue", json={"id": "7", "key": "SEC-7"}, status=201)
    return jira


def user_issue_requests(jira):
    return [c.request for c in jira.jira.calls if c.request.method == "POST" and c.request.url == f"{JIRA_API}/issue"]


async def test_without_bot_offers_all_the_users_projects(app, client, jira, no_bot):
    await connected_user(client)
    status = (await client.get("/api/digest")).json()
    assert status["configured"] and status["unavailable_reason"] is None
    assert status["filed_by"] == "subscriber" and status["bot_account"] is None
    assert [p["key"] for p in (await client.get("/api/digest/projects")).json()] == ["SEC", "PLAT", "OPS"]
    assert not any(c.request.url.startswith(BOT_API) for c in jira.jira.calls)  # no bot calls at all


async def test_without_bot_files_with_the_subscribers_connection(app, client, user_jira_writes, no_bot, db):
    await connected_user(client)
    await subscribe(client, ["SEC"])
    await backdate_subscriptions(db, 30)

    assert (await run(app)).outcome == "filed 2 tickets in SEC"
    requests = user_issue_requests(user_jira_writes)
    assert len(requests) == 2 and bot_issue_requests(user_jira_writes) == []
    assert requests[0].headers["Authorization"] == "Bearer access-1"  # the user's OAuth token
    description = json.dumps(json.loads(requests[-1].body)["fields"]["description"])
    assert "using Alice Atlassian's Jira connection, because no digest bot account is configured" in description

    from app.db.models import User

    alice = await db.scalar(select(User).where(User.email == "alice@example.com"))
    deliveries = (await db.scalars(select(DigestDelivery))).all()
    assert {d.filed_by_user_id for d in deliveries} == {alice.id}
    assert {d.site_url for d in deliveries} == {"https://acme.atlassian.net"}


async def test_without_bot_one_ticket_per_project_filed_by_the_earliest_subscriber(app, client, user_jira_writes, no_bot, db):
    from app.db.models import User

    await connected_user(client)
    await subscribe(client, ["SEC"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bob:
        await connected_user(bob, "bob@example.com")
        await subscribe(bob, ["SEC"])
    await backdate_subscriptions(db, 30)

    await run(app)
    assert len(user_issue_requests(user_jira_writes)) == 2  # two posts, once each, not once per subscriber
    alice = await db.scalar(select(User).where(User.email == "alice@example.com"))
    assert {d.filed_by_user_id for d in await db.scalars(select(DigestDelivery))} == {alice.id}


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


def filed_titles(jira) -> list[str]:
    return [json.loads(r.body)["fields"]["summary"] for r in bot_issue_requests(jira)]


async def test_files_each_post_once_per_project_as_the_bot(app, client, jira, db):
    await connected_user(client)
    await subscribe(client, ["SEC"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bob:
        await connected_user(bob, "bob@example.com")
        await subscribe(bob, ["SEC"])  # same project, second subscriber
    await backdate_subscriptions(db, 30)

    result = await run(app)
    assert result.outcome == "filed 2 tickets in SEC" and result.error is None
    # Catch-up, oldest first, one ticket per post however many people subscribed the project.
    assert filed_titles(jira) == ["NHI Blog Digest: Older featured post", "NHI Blog Digest: When a Worm Steals Your Keys"]

    request = bot_issue_requests(jira)[-1]
    assert request.headers["Authorization"].startswith("Basic ")  # created by the bot account
    fields = json.loads(request.body)["fields"]
    assert fields["project"] == {"key": "SEC"}
    assert fields["labels"] == ["identityhub", "nhi-blog-digest"]
    description = json.dumps(fields["description"])
    assert "explains why rotating credentials matters" in description
    assert '"href": "https://blog.test/blog/newest"' in description
    assert "Summary: extractive summary (no LLM configured)." in description

    # Running again files nothing new.
    assert (await run(app)).outcome.startswith("up to date")
    assert len(bot_issue_requests(jira)) == 2
    status = (await client.get("/api/digest")).json()
    assert status["subscriptions"][0]["last_ticket"]["post_title"] == "When a Worm Steals Your Keys"


async def test_fresh_start_only_files_posts_published_after_subscribing(app, client, jira, db):
    await connected_user(client)
    await subscribe(client, ["SEC"])  # just now: both blog posts are older

    assert (await run(app)).outcome.startswith("up to date")
    assert bot_issue_requests(jira) == []

    publish_new_post(jira, age_days=-0.01)  # published after the subscription
    assert (await run(app)).outcome == "filed 1 ticket in SEC"
    assert filed_titles(jira) == ["NHI Blog Digest: Brand new post"]


async def subscribe_sending_latest(client, keys):
    return await client.put(
        "/api/digest/subscriptions", json={"project_keys": keys, "send_latest_now": True}, headers=await csrf(client)
    )


async def test_send_latest_now_files_just_the_latest_post_right_away(app, client, jira):
    await connected_user(client)
    assert (await subscribe_sending_latest(client, ["SEC"])).status_code == 200
    await app.state.digest.task  # saving started a run

    # The current latest post, not the older backlog.
    assert filed_titles(jira) == ["NHI Blog Digest: When a Worm Steals Your Keys"]
    assert app.state.digest.last_run.outcome == "filed 1 ticket in SEC"


async def test_send_latest_now_only_applies_to_newly_added_projects(app, client, jira):
    await connected_user(client)
    await subscribe(client, ["SEC"])  # fresh start, no backlog
    assert app.state.digest.task is None  # no run without the option

    await subscribe_sending_latest(client, ["SEC", "OPS"])
    await app.state.digest.task
    [request] = bot_issue_requests(jira)
    assert json.loads(request.body)["fields"]["project"] == {"key": "OPS"}


async def test_catch_up_files_only_posts_newer_than_the_last_digest(app, client, jira, db):
    await connected_user(client)
    await subscribe(client, ["SEC"])
    await backdate_subscriptions(db, 30)
    await run(app)  # files the two existing posts
    publish_new_post(jira)

    assert (await run(app)).outcome == "filed 1 ticket in SEC"
    assert filed_titles(jira)[-1] == "NHI Blog Digest: Brand new post"


async def test_each_post_is_summarized_once_and_stored(app, client, jira, db, monkeypatch):
    from app.digest.summarizers import ExtractiveSummarizer

    calls = []
    original = ExtractiveSummarizer.summarize

    async def counting(self, post):
        calls.append(post.title)
        return await original(self, post)

    monkeypatch.setattr(ExtractiveSummarizer, "summarize", counting)
    await connected_user(client)
    await subscribe(client, ["SEC"])
    await backdate_subscriptions(db, 30)
    await run(app)
    assert sorted(calls) == ["Older featured post", "When a Worm Steals Your Keys"]

    # A second project subscribes later and catches up on the same posts: no new model calls.
    await subscribe(client, ["SEC", "OPS"])
    await backdate_subscriptions(db, 30)
    assert (await run(app)).outcome == "filed 2 tickets in OPS"
    assert len(calls) == 2
    assert await db.scalar(select(func.count()).select_from(DigestPost)) == 2
    stored = await db.scalar(select(DigestPost).where(DigestPost.title == "Older featured post"))
    assert stored.summary.startswith("Key sentences from the post:") and stored.summarizer == "extractive"


async def test_catch_up_is_capped_per_run(app, client, jira, db, monkeypatch):
    from app.services import digest

    monkeypatch.setattr(digest, "MAX_POSTS_PER_PROJECT_PER_RUN", 1)
    await connected_user(client)
    await subscribe(client, ["SEC"])
    await backdate_subscriptions(db, 30)
    assert (await run(app)).outcome == "filed 1 ticket in SEC"
    assert (await run(app)).outcome == "filed 1 ticket in SEC"
    assert filed_titles(jira) == ["NHI Blog Digest: Older featured post", "NHI Blog Digest: When a Worm Steals Your Keys"]


async def test_failed_summary_stops_the_project_and_is_retried(app, client, jira, db, monkeypatch):
    from app.digest.summarizers import ExtractiveSummarizer, SummaryError

    original = ExtractiveSummarizer.summarize

    async def flaky(self, post):
        if post.title == "Older featured post":
            raise SummaryError("model timed out")
        return await original(self, post)

    monkeypatch.setattr(ExtractiveSummarizer, "summarize", flaky)
    await connected_user(client)
    await subscribe(client, ["SEC"])
    await backdate_subscriptions(db, 30)

    result = await run(app)
    assert result.outcome == "nothing filed"  # the newer post waits, so order is kept
    sub = await db.scalar(select(DigestSubscription).execution_options(populate_existing=True))
    assert sub.last_error == "The summary of 'Older featured post' couldn't be generated: model timed out"

    monkeypatch.setattr(ExtractiveSummarizer, "summarize", original)
    assert (await run(app)).outcome == "filed 2 tickets in SEC"
    sub = await db.scalar(select(DigestSubscription).execution_options(populate_existing=True))
    assert sub.last_error is None


async def test_not_filed_when_no_subscriber_still_has_access(app, client, jira, db):
    await connected_user(client)
    await subscribe(client, ["SEC"])
    await backdate_subscriptions(db, 30)
    jira.jira.replace("GET", f"{JIRA_API}/project/search", json={"values": []})  # alice lost access

    result = await run(app)
    assert result.outcome == "nothing filed" and bot_issue_requests(jira) == []
    sub = await db.scalar(select(DigestSubscription).execution_options(populate_existing=True))
    assert sub.last_error == "You can no longer create issues in SEC, so the digest wasn't filed there."


async def test_bot_losing_access_is_reported_on_the_subscription(app, client, jira, db):
    await connected_user(client)
    await subscribe(client, ["SEC"])
    await backdate_subscriptions(db, 30)
    jira.jira.replace("POST", f"{BOT_API}/issue", json={"errorMessages": ["Forbidden"]}, status=403)

    result = await run(app)
    assert result.error == "Problems in SEC; see the subscription errors."
    sub = await db.scalar(select(DigestSubscription).execution_options(populate_existing=True))
    assert "The IdentityHub bot can no longer create issues in SEC" in sub.last_error
    assert await db.scalar(select(func.count()).select_from(DigestDelivery)) == 0  # retried next run


async def test_blog_unreachable_fails_cleanly(app, client, jira):
    await connected_user(client)
    await subscribe(client, ["SEC"])
    jira.oauth.get(BLOG).mock(side_effect=httpx.ConnectError("down"))
    result = await run(app)
    assert result.outcome == "failed" and "Couldn't fetch" in result.error


async def test_run_now_endpoint(app, client, jira, db):
    await connected_user(client)
    await subscribe(client, ["SEC"])
    await backdate_subscriptions(db, 30)
    response = await client.post("/api/digest/run", headers=await csrf(client))
    assert response.status_code == 202
    await app.state.digest.task
    assert (await client.get("/api/digest")).json()["last_run"]["outcome"] == "filed 2 tickets in SEC"


async def test_subscribing_during_a_run_triggers_another_pass(app, monkeypatch):
    """A run in progress has already read the subscriptions; "send the latest post now" must not
    wait until tomorrow."""
    import asyncio

    from app.api.digest import _start_run
    from app.core.config import get_settings
    from app.services import digest
    from app.services.digest import LastRun

    release, passes = asyncio.Event(), []

    async def slow_run(settings, runtime):
        passes.append(1)
        await release.wait()
        return LastRun(datetime.now(UTC), "ok")

    monkeypatch.setattr(digest, "_run", slow_run)
    runtime = app.state.digest
    assert _start_run(get_settings(), runtime)
    await asyncio.sleep(0)
    assert not _start_run(get_settings(), runtime, rerun_if_running=True)
    release.set()
    await runtime.task
    assert len(passes) == 2 and not runtime.running


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        ("2026-10-06T08:59:00+00:00", "2026-10-06T09:00:00+00:00"),  # later today
        ("2026-10-06T09:00:00+00:00", "2026-10-07T09:00:00+00:00"),  # exactly now: tomorrow
        ("2026-10-06T23:30:00+00:00", "2026-10-07T09:00:00+00:00"),
        ("2026-10-06T12:00:00+03:00", "2026-10-07T09:00:00+00:00"),  # 09:00 UTC already passed
    ],
)
def test_next_run_time_is_a_fixed_utc_time(now, expected):
    from app.services.digest import next_run_at

    assert next_run_at("09:00", datetime.fromisoformat(now)) == datetime.fromisoformat(expected)


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
