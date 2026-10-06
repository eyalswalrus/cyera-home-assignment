#!/usr/bin/env python3
"""Create a `.env` at the repo root from `.env.example`, filling in freshly generated secrets.

Uses only the standard library so it can run before any dependencies are installed.
Refuses to overwrite an existing `.env`.
"""

import base64
import os
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
    # Create the file owner-only from the start, so the secrets are never world-readable.
    fd = os.open(TARGET, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Created {TARGET} with generated SECRET_KEY and ENCRYPTION_KEYS.")
    print("Next: add your Atlassian OAuth app credentials (ATLASSIAN_CLIENT_ID / ATLASSIAN_CLIENT_SECRET).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
