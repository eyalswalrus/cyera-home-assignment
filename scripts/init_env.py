#!/usr/bin/env python3
"""Create a `.env` at the repo root from `.env.example`, filling in freshly generated secrets.

Uses only the standard library so it can run before any dependencies are installed.
Refuses to overwrite an existing `.env`.
"""

import base64
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"
TARGET = ROOT / ".env"


def main() -> int:
    if TARGET.exists():
        print(f"{TARGET} already exists - leaving it untouched.")
        return 0

    generated = {
        "SECRET_KEY": secrets.token_urlsafe(48),
        # A Fernet key is 32 random bytes, url-safe base64 encoded.
        "ENCRYPTION_KEYS": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
    }

    lines = []
    for line in EXAMPLE.read_text().splitlines():
        key = line.split("=", 1)[0]
        lines.append(f"{key}={generated[key]}" if key in generated else line)
    TARGET.write_text("\n".join(lines) + "\n")
    TARGET.chmod(0o600)

    print(f"Created {TARGET} with generated SECRET_KEY and ENCRYPTION_KEYS.")
    print("Next: add your Atlassian OAuth app credentials (ATLASSIAN_CLIENT_ID / ATLASSIAN_CLIENT_SECRET).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
