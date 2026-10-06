# IdentityHub - NHI findings → Jira

Proof-of-concept integration that lets IdentityHub users report Non-Human Identity findings
(stale service accounts, over-privileged keys, expiring credentials, …) to their Jira Cloud
workspace - from the UI or programmatically via a REST API.

> **Status:** work in progress. Sections marked _TBD_ are filled in as features land.
> See [DESIGN.md](DESIGN.md) for the reasoning behind the design.

## Quick start

### 1. Create the Atlassian OAuth app (~5 minutes)

You need a Jira Cloud site (a free one from [atlassian.com](https://www.atlassian.com/software/jira/free)
works) and an OAuth 2.0 app that IdentityHub signs in through:

1. Open the [Atlassian developer console](https://developer.atlassian.com/console/myapps/) →
   **Create** → **OAuth 2.0 integration**, and give it a name (e.g. "IdentityHub local").
2. **Permissions** → **Jira API** → **Add**, then **Configure** and add the classic scopes
   `read:jira-work`, `write:jira-work` and `read:jira-user`.
   (`offline_access`, for refresh tokens, is requested at sign-in and needs no setup.)
3. **Authorization** → **OAuth 2.0 (3LO)** → set the callback URL to
   `http://localhost:8000/api/jira/callback`.
4. **Settings** → copy the **Client ID** and **Secret** for step 2 below.

By default an Atlassian OAuth app can only be authorized by its owner. To connect a second
Atlassian account (for example to check that two users see different projects), set
**Distribution** → **Sharing** in the console.

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
# When using the Vite dev server, set UI_BASE_URL=http://localhost:5173 in .env so the
# Jira OAuth callback returns you to it.

# Tests
cd server && uv run pytest
cd web && npm test

# After changing the API: regenerate the frontend's TypeScript types from FastAPI's schema
cd web && npm run gen:api
```

Dependencies are locked on both sides (`server/uv.lock`, `web/package-lock.json`), and the
Docker build installs from those lock files only (`uv sync --locked`, `npm ci`).

## Architecture

```
web/      React + Vite + TypeScript SPA - UI only, talks to /api
  src/api/        typed API client (generated from OpenAPI), React Query hooks, error mapping
  src/pages/      Sign in, Create account, Report finding, Settings
  src/components/ project picker, finding form, recent tickets, Jira status
server/   FastAPI backend
  app/api/       HTTP layer: request validation → service call → response
  app/services/  business logic (findings, Jira connection, API keys)
  app/jira/      Jira Cloud client wrapper + error translation
  app/db/        SQLAlchemy models and session
  app/schemas/   request/response contracts shared by the UI and REST API
  app/core/      settings, encryption, CSRF, rate limiting, security headers
scripts/  helper scripts (env bootstrap)
```

In production, FastAPI serves the built UI, so the browser talks to a single origin: no CORS,
and session cookies stay first-party.

## Design choices

Decisions, alternatives considered, security model and known limitations are documented in
[DESIGN.md](DESIGN.md).
