"""Encryption at rest for secrets stored in the database (e.g. Jira OAuth tokens)."""

import json
import logging
from functools import lru_cache
from typing import Any

from cryptography.fernet import InvalidToken, MultiFernet
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.core.config import build_fernet, get_settings

logger = logging.getLogger(__name__)


@lru_cache
def get_fernet() -> MultiFernet:
    return build_fernet(get_settings().encryption_keys.get_secret_value())


class EncryptedJSON(TypeDecorator[dict[str, Any]]):
    """Stores a JSON-serialisable dict as a Fernet token (AES-128-CBC + HMAC-SHA256).

    Encryption happens at the ORM boundary, so application code only ever sees plaintext dicts
    and the database file (or a backup of it) only ever contains ciphertext.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: dict[str, Any] | None, dialect: Any) -> str | None:
        if value is None:
            return None
        return get_fernet().encrypt(json.dumps(value).encode()).decode()

    def process_result_value(self, value: str | None, dialect: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        try:
            return json.loads(get_fernet().decrypt(value.encode()))
        except InvalidToken:
            # Encrypted with a key that's no longer configured. Surface as "no value" so callers
            # can ask the user to reconnect, instead of failing every query that loads the row.
            logger.warning("Could not decrypt a stored secret; was ENCRYPTION_KEYS changed?")
            return None
