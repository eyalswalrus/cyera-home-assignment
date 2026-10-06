from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# digest/digest/config.py -> repo root. Inside the Docker image no such file exists; env vars are used.
_REPO_ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    """Configuration from environment variables (or a `.env` file next to where it runs).

    Claude credentials are resolved by the Anthropic SDK itself (ANTHROPIC_API_KEY, or an
    `ant auth login` profile), so they don't appear here.
    """

    # The repo-root .env (shared with the IdentityHub server), then a local .env if present.
    model_config = SettingsConfigDict(env_file=(_REPO_ROOT_ENV, ".env"), extra="ignore")

    # IdentityHub's public REST API, and an API key allowed to post to `digest_project_key`.
    # Required to file tickets; not needed for --dry-run.
    identityhub_url: str = "http://localhost:8000"
    identityhub_api_key: SecretStr | None = None
    digest_project_key: str | None = None

    blog_url: str = "https://www.oasis.security/blog"
    # How many posts from the top of the blog index to compare by publish date. The index pins a
    # featured post first, so "first link" is not reliably "newest".
    blog_candidates: int = 8
    # Remembers the last post turned into a ticket, so re-runs don't create duplicates.
    state_file: Path = Path(".digest-state.json")

    claude_model: str = "claude-opus-5-5"

    def missing_for_filing(self) -> list[str]:
        return [
            name
            for name, value in (("IDENTITYHUB_API_KEY", self.identityhub_api_key), ("DIGEST_PROJECT_KEY", self.digest_project_key))
            if not value
        ]
