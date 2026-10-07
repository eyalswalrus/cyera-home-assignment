"""Data retention: a daily job that deletes data IdentityHub no longer needs.

* `finding` rows (the audit record of created tickets) older than FINDING_RETENTION_DAYS.
* Expired login sessions. They are already rejected on use; this removes the rows.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from app.auth.users import SESSION_LIFETIME_SECONDS
from app.core.config import Settings
from app.db.models import AccessToken, Finding
from app.db.session import session_scope

log = logging.getLogger(__name__)

INTERVAL = timedelta(days=1)


@dataclass(frozen=True)
class Purged:
    findings: int
    sessions: int


async def purge_expired_data(settings: Settings, now: datetime | None = None) -> Purged:
    now = now or datetime.now(UTC)
    async with session_scope() as db:
        findings = await db.execute(
            delete(Finding).where(Finding.created_at < now - timedelta(days=settings.finding_retention_days))
        )
        sessions = await db.execute(
            delete(AccessToken).where(AccessToken.created_at < now - timedelta(seconds=SESSION_LIFETIME_SECONDS))
        )
        await db.commit()
    purged = Purged(findings=findings.rowcount or 0, sessions=sessions.rowcount or 0)
    if purged.findings or purged.sessions:
        log.info("Retention: deleted %d finding record(s) and %d expired session(s)", purged.findings, purged.sessions)
    return purged


async def schedule(settings: Settings, startup_delay_seconds: float = 30) -> None:
    """Shortly after startup, then daily. Purging is idempotent, so running it on every replica
    would be harmless, if wasteful."""
    await asyncio.sleep(startup_delay_seconds)
    while True:
        try:
            await purge_expired_data(settings)
        except Exception:  # keep the schedule alive; the next run retries
            log.exception("Retention job failed")
        await asyncio.sleep(INTERVAL.total_seconds())
