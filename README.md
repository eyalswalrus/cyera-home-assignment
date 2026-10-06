# IdentityHub - NHI findings → Jira

Proof-of-concept integration that lets IdentityHub users report Non-Human Identity findings
(stale service accounts, over-privileged keys, expiring credentials, …) to their Jira Cloud
workspace - from the UI or programmatically via a REST API.

> **Status:** work in progress. Sections marked _TBD_ are filled in as features land.
> See [DESIGN.md](DESIGN.md) for the reasoning behind the design.

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

Requires [uv](https://docs.astral.sh/uv/) and Node 20+. uv installs the pinned Python version
and the exact dependency versions from `server/uv.lock`.

```bash
# Backend (http://localhost:8000)
cd server
uv sync
uv run uvicorn app.main:app --reload

# Frontend (http://localhost:5173, proxies /api to the backend)
cd web
npm install && npm run dev

# Tests
cd server && uv run pytest
```

Dependencies are locked on both sides (`server/uv.lock`, `web/package-lock.json`), and the
Docker build installs from those lock files only (`uv sync --locked`, `npm ci`).

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

## Design choices

Decisions, alternatives considered, security model and known limitations are documented in
[DESIGN.md](DESIGN.md).
