"""Re-encrypt all stored secrets with the current primary encryption key.

Key rotation procedure:
  1. Prepend a new Fernet key to ENCRYPTION_KEYS (new writes use it; old rows still decrypt).
  2. Run `python -m app.db.rotate_keys`.
  3. Remove the old key from ENCRYPTION_KEYS.
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.db.models import JiraConnection
from app.db.session import close_db, get_db


async def rotate() -> int:
    count = 0
    async for db in get_db():
        for connection in (await db.scalars(select(JiraConnection))).all():
            # Decrypted on load (any key); marking it dirty re-encrypts it with the primary key.
            flag_modified(connection, "token")
            count += 1
        await db.commit()
    return count


async def main() -> None:
    try:
        count = await rotate()
    finally:
        await close_db()
    print(f"Re-encrypted {count} Jira connection(s) with the primary key.")


if __name__ == "__main__":
    asyncio.run(main())
