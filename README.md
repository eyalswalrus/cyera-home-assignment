# IdentityHub - NHI findings → Jira

Proof-of-concept integration that lets IdentityHub users report Non-Human Identity findings
(stale service accounts, over-privileged keys, expiring credentials, …) to their Jira Cloud
workspace - from the UI or programmatically via a REST API.

> **Status:** work in progress. Sections marked _TBD_ are filled in as features land.

## Quick start

### 1. Create the Atlassian OAuth app (~5 minutes)

_TBD - step-by-step with screenshots/links. Callback URL: `http://localhost:8000/api/jira/callback`._

### 2. Configure

```bash
python3 scripts/init_env.py   # creates .env with generated secrets
```

Then set `ATLASSIAN_CLIENT_ID` and `ATLASSIAN_CLIENT_SECRET` in `.env`.

### 3. Run

```bash
docker compose up --build
```

Open http://localhost:8000.

### Local development (without Docker)

Requires Python 3.12+ and Node 20+.

```bash
# Backend (http://localhost:8000)
cd server
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/uvicorn app.main:app --reload

# Frontend (http://localhost:5173, proxies /api to the backend)
cd web
npm install && npm run dev

# Tests
cd server && .venv/bin/pytest
```

## Architecture

```
web/      React + Vite + TypeScript SPA - UI only, talks to /api
server/   FastAPI backend
  app/api/       HTTP layer: request validation → service call → response
  app/services/  business logic (findings, Jira connection, API keys)
  app/jira/      Jira Cloud client wrapper + error translation
  app/db/        SQLAlchemy models and session
  app/core/      settings, encryption
scripts/  helper scripts (env bootstrap)
```

In production, FastAPI serves the built UI, so the browser talks to a single origin: no CORS,
and session cookies stay first-party.

### Libraries (no reinvented wheels)

| Concern | Library |
|---|---|
| App accounts, password hashing, sessions | fastapi-users (DB-backed session tokens) |
| Jira OAuth 2.0 (3LO) | Authlib |
| Jira REST calls | atlassian-python-api (`JiraCloud`) |
| CSRF | starlette-csrf |
| Rate limiting | slowapi |
| Encryption at rest | cryptography (`MultiFernet`) |
| Config | pydantic-settings |

## Security notes

- **Multi-tenancy:** _TBD_
- **Jira credentials:** OAuth tokens are encrypted at rest with `MultiFernet` and are never sent
  to the browser. To rotate the key: prepend a new key to `ENCRYPTION_KEYS`, run
  `python -m app.db.rotate_keys` (re-encrypts every row with the new key), then remove the old key.
- **Database integrity:** SQLite foreign keys are enabled on every connection, so deleting a user
  cascades to their Jira connection, API keys and findings.
- **Sessions:** _TBD_
- **API keys:** _TBD_

## Scope decisions & assumptions

_TBD_

## Known limitations / production next steps

- Schema is created on startup (`create_all`); production would use Alembic migrations.
- SQLite is used for zero-setup; production would use Postgres.
- fastapi-users is in maintenance mode (security fixes only) - acceptable for a POC.
