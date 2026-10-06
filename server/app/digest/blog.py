"""Finding and reading the most recent post on the Oasis Security blog.

The blog has no RSS feed, so we read the HTML: post links from the index page, then each post's
schema.org `BlogPosting` data (JSON-LD) for its title and publish date, and `trafilatura` for the
article text. Posts are compared by `datePublished` because the index pins a featured post at the
top, which is not necessarily the newest one.
"""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from bs4 import BeautifulSoup

USER_AGENT = "IdentityHub-NHI-Blog-Digest/0.1"
CANDIDATES = 8  # posts from the top of the index to compare by publish date


class BlogError(Exception):
    """The blog couldn't be read or didn't look as expected."""


@dataclass(frozen=True)
class BlogPost:
    url: str
    title: str
    published: datetime
    text: str


async def fetch_latest_post(blog_url: str, candidates: int = CANDIDATES) -> BlogPost:
    return (await fetch_recent_posts(blog_url, candidates))[-1]


async def fetch_recent_posts(blog_url: str, candidates: int = CANDIDATES) -> list[BlogPost]:
    """The posts linked from the top of the blog index, oldest first (by publish date)."""
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=20) as client:
        links = _post_links(await _get(client, blog_url), blog_url)[:candidates]
        if not links:
            raise BlogError(f"No blog post links found on {blog_url}; the page layout may have changed.")
        pages = await asyncio.gather(*(_get(client, url) for url in links))
    # Parsing and text extraction are CPU work; keep them off the event loop.
    posts = await asyncio.to_thread(lambda: [p for url, html in zip(links, pages) if (p := _parse_post(url, html))])
    if not posts:
        raise BlogError("None of the blog posts had a readable title and publish date.")
    return sorted(posts, key=lambda p: p.published)


async def _get(client: httpx.AsyncClient, url: str) -> str:
    try:
        response = await client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise BlogError(f"Couldn't fetch {url}: {exc}") from exc
    return response.text


def _post_links(html: str, blog_url: str) -> list[str]:
    """Absolute post URLs directly under /blog/, in page order, without duplicates."""
    base_path = urlparse(blog_url).path.rstrip("/")
    links: dict[str, None] = {}
    for anchor in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        url = urljoin(blog_url, anchor["href"]).split("#")[0].split("?")[0]
        path = urlparse(url).path.rstrip("/")
        if path.startswith(f"{base_path}/") and path.count("/") == base_path.count("/") + 1:
            links[url] = None
    return list(links)


def _parse_post(url: str, html: str) -> BlogPost | None:
    meta = _blog_posting(html)
    if meta is None:
        return None
    try:
        published = datetime.fromisoformat(meta["datePublished"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    title = BeautifulSoup(meta.get("headline", ""), "html.parser").get_text().strip()
    text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    if not title or not text:
        return None
    return BlogPost(url=url, title=title, published=published, text=text)


def _blog_posting(html: str) -> dict | None:
    """The page's schema.org BlogPosting JSON-LD object, if any."""
    for script in BeautifulSoup(html, "html.parser").find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") in ("BlogPosting", "Article", "NewsArticle"):
                return item
    return None
