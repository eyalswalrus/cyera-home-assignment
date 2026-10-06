"""Application settings, loaded from environment variables (and the repo-root `.env` in dev)."""

from functools import lru_cache
from pathlib import Path

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
    def jira_configured(self) -> bool:
        return bool(self.atlassian_client_id and self.atlassian_client_secret)


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
