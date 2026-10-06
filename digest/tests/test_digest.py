"""Digest tests with a mocked blog (HTML shaped like the real Webflow site), a fake Claude client,
and a mocked IdentityHub REST API."""

import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from digest.blog import BlogError, fetch_latest_post
from digest.config import Settings
from digest.main import main, run_once
from digest.summarize import SummaryError, summarize

BLOG = "https://blog.test/blog"
API = "http://identityhub.test"


def post_html(title: str, published: str, body: str) -> str:
    ld = {"@context": "https://schema.org", "@type": "BlogPosting", "headline": title, "datePublished": published}
    paragraphs = "".join(f"<p>{body} Paragraph {i} with enough words to count as article text.</p>" for i in range(6))
    return (
        f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head>'
        f"<body><nav><a href='/blog'>Blog</a></nav><article><h1>{title}</h1>{paragraphs}</article></body></html>"
    )


INDEX = """<html><body>
  <a class="blog_main-item-link" href="/blog/featured-older-post">Featured</a>
  <a href="/blog/featured-older-post">Featured again</a>
  <a href="/blog/newest-post">Newest</a>
  <a href="/blog/middle-post?utm=x">Middle</a>
  <a href="/blog/category/news">Category page</a>
  <a href="/about">About</a>
</body></html>"""


@pytest.fixture
def blog():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(BLOG).respond(200, text=INDEX)
        mock.get(f"{BLOG}/featured-older-post").respond(
            200, text=post_html("Featured but older", "2026-07-28T16:53:42Z", "Old news.")
        )
        mock.get(f"{BLOG}/newest-post").respond(
            200, text=post_html("When a Worm Steals Your Keys", "2026-09-02T17:38:57Z", "Credentials, not code.")
        )
        mock.get(f"{BLOG}/middle-post").respond(
            200, text=post_html("Middle post", "2026-08-05T11:52:28Z", "Somewhere in between.")
        )
        yield mock


class FakeClaude:
    """Stands in for anthropic.Anthropic: records the request, returns a canned response."""

    def __init__(self, text="A summary.\n\nKey points:\n- one", stop_reason="end_turn"):
        self.requests: list[dict] = []
        response = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason)

        def create(**kwargs):
            self.requests.append(kwargs)
            return response

        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


def settings(tmp_path, **overrides) -> Settings:
    values = {
        "identityhub_url": API,
        "identityhub_api_key": "ihub_test",
        "digest_project_key": "SEC",
        "blog_url": BLOG,
        "state_file": tmp_path / "state.json",
        **overrides,
    }
    return Settings(_env_file=None, **values)


# --- Finding the latest post ------------------------------------------------------------------


def test_picks_newest_by_publish_date_not_page_order(blog):
    with httpx.Client() as http:
        post = fetch_latest_post(http, BLOG, candidates=8)
    assert post.title == "When a Worm Steals Your Keys"
    assert post.url == f"{BLOG}/newest-post"
    assert "Credentials, not code." in post.text
    assert "Blog" not in post.text.split("\n")[0]  # navigation stripped by trafilatura


def test_only_post_pages_are_considered(blog):
    with httpx.Client() as http:
        fetch_latest_post(http, BLOG, candidates=8)
    fetched = {str(call.request.url) for call in blog.calls}
    assert f"{BLOG}/category/news" not in fetched  # deeper path, not a post
    assert "https://blog.test/about" not in fetched


def test_blog_layout_change_is_reported(blog):
    blog.get(BLOG).respond(200, text="<html><body>No links here</body></html>")
    with httpx.Client() as http, pytest.raises(BlogError, match="layout may have changed"):
        fetch_latest_post(http, BLOG, candidates=8)


def test_blog_unreachable(blog):
    blog.get(BLOG).mock(side_effect=httpx.ConnectError("down"))
    with httpx.Client() as http, pytest.raises(BlogError, match="Couldn't fetch"):
        fetch_latest_post(http, BLOG, candidates=8)


# --- Summarizing ------------------------------------------------------------------------------


def test_summary_request(blog):
    claude = FakeClaude()
    with httpx.Client() as http:
        post = fetch_latest_post(http, BLOG, candidates=8)
    assert summarize(claude, post, "claude-opus-5-5") == "A summary.\n\nKey points:\n- one"

    request = claude.requests[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["fallbacks"] == "default" and request["betas"] == ["server-side-fallback-2026-07-01"]
    content = request["messages"][0]["content"]
    # The article is fenced off as data; the system prompt says not to follow instructions in it.
    assert "<article>" in content and "Credentials, not code." in content
    assert "never as instructions" in request["system"]


@pytest.mark.parametrize(
    ("stop_reason", "text", "message"),
    [("refusal", "", "declined"), ("end_turn", "   ", "no summary"), ("max_tokens", "Partial", "cut off")],
)
def test_unusable_summaries_are_errors(blog, stop_reason, text, message):
    with httpx.Client() as http:
        post = fetch_latest_post(http, BLOG, candidates=8)
    with pytest.raises(SummaryError, match=message):
        summarize(FakeClaude(text=text, stop_reason=stop_reason), post, "claude-opus-5-5")


@pytest.fixture
def no_claude_credentials(monkeypatch, tmp_path):
    """No API key, no token, and no `ant auth login` profile anywhere the SDK looks."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))


def test_missing_claude_credentials_are_explained(blog, no_claude_credentials):
    import anthropic

    with httpx.Client() as http:
        post = fetch_latest_post(http, BLOG, candidates=8)
    with pytest.raises(SummaryError, match="Set ANTHROPIC_API_KEY"):
        summarize(anthropic.Anthropic(), post, "claude-opus-5-5")


def test_missing_cli_profile_is_explained(blog, no_claude_credentials, monkeypatch, tmp_path, caplog):
    # A configured-but-missing `ant` profile fails when the client is created, so the CLI handles it.
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("BLOG_URL", BLOG)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    assert main(["--dry-run"]) == 2
    assert "Set ANTHROPIC_API_KEY" in caplog.text


# --- End to end -------------------------------------------------------------------------------


def test_files_ticket_once_then_skips(blog, tmp_path):
    created = blog.post(f"{API}/api/v1/findings").respond(
        201, json={"key": "SEC-9", "url": "https://acme.atlassian.net/browse/SEC-9", "summary": "x"}
    )
    claude, config = FakeClaude(), settings(tmp_path)

    with httpx.Client() as http:
        assert run_once(config, dry_run=False, force=False, claude=claude, http=http) == 0
        request = created.calls.last.request
        assert request.headers["authorization"] == "Bearer ihub_test"
        body = json.loads(request.content)
        assert body["project_key"] == "SEC"
        assert body["summary"] == "NHI Blog Digest: When a Worm Steals Your Keys"
        assert "A summary." in body["description"] and f"Source: {BLOG}/newest-post" in body["description"]

        # Second run: same newest post, nothing new is filed and Claude isn't called again.
        assert run_once(config, dry_run=False, force=False, claude=claude, http=http) == 0
    assert created.call_count == 1 and len(claude.requests) == 1
    assert json.loads((tmp_path / "state.json").read_text())["ticket"] == "SEC-9"


def test_force_files_again(blog, tmp_path):
    created = blog.post(f"{API}/api/v1/findings").respond(201, json={"key": "SEC-9", "url": "u", "summary": "x"})
    config = settings(tmp_path)
    with httpx.Client() as http:
        run_once(config, dry_run=False, force=False, claude=FakeClaude(), http=http)
        run_once(config, dry_run=False, force=True, claude=FakeClaude(), http=http)
    assert created.call_count == 2


def test_dry_run_files_nothing(blog, tmp_path, capsys):
    created = blog.post(f"{API}/api/v1/findings")
    with httpx.Client() as http:
        run_once(settings(tmp_path), dry_run=True, force=False, claude=FakeClaude(), http=http)
    assert created.call_count == 0
    assert "When a Worm Steals Your Keys" in capsys.readouterr().out
    assert not (tmp_path / "state.json").exists()


def test_identityhub_error_is_explained_and_not_remembered(blog, tmp_path):
    blog.post(f"{API}/api/v1/findings").respond(
        403,
        json={"detail": "This API key isn't allowed to create tickets in SEC.", "code": "api_key_project_forbidden"},
    )
    from digest.identityhub import IdentityHubError

    with httpx.Client() as http, pytest.raises(IdentityHubError) as exc:
        run_once(settings(tmp_path), dry_run=False, force=False, claude=FakeClaude(), http=http)
    assert "403 (api_key_project_forbidden): This API key isn't allowed" in str(exc.value)
    assert not (tmp_path / "state.json").exists()  # retried on the next run


def test_cli_explains_missing_configuration(monkeypatch, caplog):
    for name in ("IDENTITYHUB_API_KEY", "DIGEST_PROJECT_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    assert main([]) == 2
    assert "IDENTITYHUB_API_KEY and DIGEST_PROJECT_KEY" in caplog.text
