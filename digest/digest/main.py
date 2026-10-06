"""NHI Blog Digest: fetch the newest Oasis Security blog post, summarize it with Claude, and file it
as a Jira ticket through IdentityHub's REST API.

    digest                       # run once (skips a post already filed)
    digest --dry-run             # fetch + summarize, print instead of creating a ticket
    digest --force               # file the newest post even if it was filed before
    digest --every-hours 24      # keep running, checking once a day
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import anthropic
import httpx

from digest.blog import BlogError, fetch_latest_post
from digest.config import Settings
from digest.identityhub import IdentityHubError, create_ticket
from digest.summarize import NO_CREDENTIALS, SummaryError, summarize

log = logging.getLogger("digest")


def run_once(settings: Settings, *, dry_run: bool, force: bool, claude: anthropic.Anthropic, http: httpx.Client) -> int:
    post = fetch_latest_post(http, settings.blog_url, settings.blog_candidates)
    log.info("Latest post: %r (%s) %s", post.title, f"{post.published:%Y-%m-%d}", post.url)

    if not force and _last_filed(settings.state_file) == post.url:
        log.info("Already filed this post; nothing to do (use --force to file it again).")
        return 0

    summary = summarize(claude, post, settings.claude_model)
    if dry_run:
        print(f"--- {post.title}\n{post.url}\n\n{summary}")
        return 0

    assert settings.identityhub_api_key and settings.digest_project_key  # checked in main()
    ticket = create_ticket(
        http,
        settings.identityhub_url,
        settings.identityhub_api_key.get_secret_value(),
        settings.digest_project_key,
        post,
        summary,
    )
    _remember(settings.state_file, post.url, ticket.key)
    log.info("Created %s: %s", ticket.key, ticket.url)
    return 0


def _last_filed(path: Path) -> str | None:
    try:
        return json.loads(path.read_text()).get("last_post_url")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _remember(path: Path, post_url: str, ticket_key: str) -> None:
    path.write_text(json.dumps({"last_post_url": post_url, "ticket": ticket_key}, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize the latest Oasis Security blog post into a Jira ticket.")
    parser.add_argument("--dry-run", action="store_true", help="print the summary instead of creating a ticket")
    parser.add_argument("--force", action="store_true", help="file the newest post even if it was filed already")
    parser.add_argument("--every-hours", type=float, help="keep running, checking at this interval")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    settings = Settings()
    if not args.dry_run and (missing := settings.missing_for_filing()):
        log.error(
            "Set %s to file tickets: create an API key in IdentityHub under Settings → API keys, "
            "allowed to post to the digest project. (Or use --dry-run to only print the summary.)",
            " and ".join(missing),
        )
        return 2

    try:
        claude = anthropic.Anthropic()
    except anthropic.CredentialsError as exc:  # e.g. a configured `ant` profile that doesn't exist
        log.error("%s %s", exc, NO_CREDENTIALS)
        return 2
    with httpx.Client() as http:
        while True:
            try:
                status = run_once(settings, dry_run=args.dry_run, force=args.force, claude=claude, http=http)
            except (BlogError, SummaryError, IdentityHubError) as exc:
                log.error("%s", exc)
                status = 1
            if args.every_hours is None:
                return status
            # A failed run is retried on the next tick rather than stopping the schedule.
            log.info("Next check in %s hours.", args.every_hours)
            time.sleep(args.every_hours * 3600)


if __name__ == "__main__":
    sys.exit(main())
