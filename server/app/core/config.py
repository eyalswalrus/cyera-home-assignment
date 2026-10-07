"""Application settings, loaded from environment variables (and the repo-root `.env` in dev)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet, MultiFernet
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# server/app/core/config.py -> repo root. In the Docker image this resolves to `/`, where no
# .env exists, so configuration comes purely from the container environment.
_REPO_ROOT_ENV = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_REPO_ROOT_ENV, env_file_encoding="utf-8", extra="ignore")

    # Public URL the browser uses to reach the app. Drives OAuth redirect URIs and cookie flags.
    app_base_url: str = "http://localhost:8000"
    # Where the browser UI lives, if different (Vite dev server: http://localhost:5173).
    # After the Jira OAuth callback the user is sent back here.
    ui_base_url: str | None = None
    database_url: str = "sqlite+aiosqlite:///./data/identityhub.db"
    # Directory containing the built React app. Unset in dev, where Vite serves the UI.
    static_dir: Path | None = None

    # Signs session/CSRF cookies.
    secret_key: SecretStr = Field(min_length=32)
    # Comma-separated Fernet keys used to encrypt Jira tokens at rest. The first key encrypts;
    # all keys decrypt, so a new key can be prepended to rotate without losing existing data.
    encryption_keys: SecretStr

    # Atlassian OAuth 2.0 (3LO) app credentials. Optional so the app still boots without them;
    # the UI then explains that the Jira integration has not been configured.
    atlassian_client_id: str | None = None
    atlassian_client_secret: SecretStr | None = None

    # --- NHI Blog Digest -------------------------------------------------------------------------
    # Optional Jira account that files digest tickets (a dedicated "IdentityHub" bot user),
    # authenticated with an Atlassian API token. Without it, tickets are filed with a subscriber's
    # own Jira connection.
    digest_jira_site_url: str | None = None  # e.g. https://acme.atlassian.net
    digest_jira_email: str | None = None
    digest_jira_api_token: SecretStr | None = None
    digest_blog_url: str = "https://www.oasis.security/blog"
    # Daily run time, HH:MM in UTC. The server also runs a catch-up shortly after it starts.
    digest_daily_at: str = Field(default="09:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    # Who writes the summary. "auto" picks the first available: Claude (if ANTHROPIC_API_KEY is
    # set), then a local Ollama model (if reachable), then a built-in extractive summary.
    llm_provider: Literal["auto", "anthropic", "ollama", "extractive"] = "auto"
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-opus-5-5"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2:3b"

    @field_validator("encryption_keys")
    @classmethod
    def _validate_encryption_keys(cls, value: SecretStr) -> SecretStr:
        try:
            build_fernet(value.get_secret_value())
        except ValueError as exc:
            raise ValueError(
                "must be one or more comma-separated Fernet keys (run `python3 scripts/init_env.py` to generate one)"
            ) from exc
        return value

    @property
    def cookie_secure(self) -> bool:
        return self.app_base_url.startswith("https://")

    @property
    def ui_url(self) -> str:
        return (self.ui_base_url or self.app_base_url).rstrip("/")

    @property
    def jira_configured(self) -> bool:
        return bool(self.atlassian_client_id and self.atlassian_client_secret)

    @property
    def digest_bot_configured(self) -> bool:
        return bool(self.digest_jira_site_url and self.digest_jira_email and self.digest_jira_api_token)


def build_fernet(keys: str) -> MultiFernet:
    parsed = [Fernet(key.strip()) for key in keys.split(",") if key.strip()]
    if not parsed:
        raise ValueError("no keys provided")
    return MultiFernet(parsed)


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        problems = "\n".join(
            f"  - {'_'.join(str(p) for p in err['loc']).upper()}: {err['msg']}" for err in exc.errors()
        )
        raise SystemExit(
            f"IdentityHub configuration is invalid:\n{problems}\n"
            "Copy .env.example to .env (or run `python3 scripts/init_env.py`) and fill in the missing values."
        ) from None
