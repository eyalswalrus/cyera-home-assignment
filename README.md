# IdentityHub - NHI findings → Jira

Proof-of-concept integration that lets IdentityHub users report Non-Human Identity findings
(stale service accounts, over-privileged keys, expiring credentials, …) to their Jira Cloud
workspace - from the UI or programmatically via a REST API.

The reasoning behind every decision, the alternatives considered and the known limitations are in
**[DESIGN.md](DESIGN.md)**.

## What's included

| Requirement from the brief | Where |
|---|---|
| Login, logout, secure sessions, concurrent users | Sign in / Create account; server-side sessions, CSRF protection, per-user data isolation (DESIGN §2, §3, §7) |
| Connect Jira after logon | Settings → Jira connection, OAuth 2.0 (3LO) per user (DESIGN §4) |
| Choose a project and create an NHI finding ticket | Report finding: searchable project picker, title, description, plus optional finding type, severity and affected identity (DESIGN §5) |
| 10 most recent tickets created from the app | Report finding → Recent tickets, each opening in Jira in a new tab |
| REST API with an API key | `POST /api/v1/findings`; keys with expiry, per-project permissions and notes under Settings → API keys (DESIGN §8) |
| Bonus: NHI Blog Digest | Settings → NHI Blog Digest; a scheduled job files each new Oasis Security blog post with an AI summary (DESIGN §9) |

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

Then set `ATLASSIAN_CLIENT_ID` and `ATLASSIAN_CLIENT_SECRET` in `.env`. Without them the app
still runs and explains that Jira isn't configured. The optional blog digest has its own settings,
covered [below](#bonus-nhi-blog-digest).

### 3. Run

```bash
docker compose up --build
```

Open http://localhost:8000, create an account, then **Settings → Connect Jira**.

To start again from an empty database: `docker compose down -v`.

### Local development (without Docker)

Requires [uv](https://docs.astral.sh/uv/) and Node 22.12+. uv installs the pinned Python version
and the exact dependency versions from `server/uv.lock`. From the repository root:

```bash
# Backend on http://localhost:8000
(cd server && uv sync && uv run uvicorn app.main:app --reload)

# Frontend on http://localhost:5173 (proxies /api to the backend). Set
# UI_BASE_URL=http://localhost:5173 in .env so the Jira OAuth callback returns you to it.
(cd web && npm install && npm run dev)

# Tests
(cd server && uv run pytest)
(cd web && npm test)

# After changing the API: regenerate the frontend's TypeScript types from FastAPI's schema
(cd web && npm run gen:api)
```

Dependencies are locked on both sides (`server/uv.lock`, `web/package-lock.json`), and the
Docker build installs from those lock files only (`uv sync --locked`, `npm ci`).

## Using the REST API

Connect Jira first, then create a key under **Settings → API keys**: choose the projects it may
post to and how long it lives. The key is shown once. Then:

```bash
curl -X POST http://localhost:8000/api/v1/findings \
  -H "Authorization: Bearer ihub_..." \
  -H "Content-Type: application/json" \
  -d '{"project_key": "SEC", "summary": "Stale Service Account: svc-deploy-prod", "severity": "high"}'
```

Interactive documentation for every endpoint and error is at http://localhost:8000/docs.

## Bonus: NHI Blog Digest

New posts on the [Oasis Security blog](https://www.oasis.security/blog) are summarized and filed
as Jira tickets in the projects users choose under **Settings → NHI Blog Digest**. To enable it:

1. Create a dedicated Atlassian account for the bot (e.g. "IdentityHub"; Jira's free plan allows
   10 users), give it *Create Issues* in the projects that may receive the digest, and create an
   [API token](https://id.atlassian.com/manage-profile/security/api-tokens) for it.
2. In `.env`, set `DIGEST_JIRA_SITE_URL`, `DIGEST_JIRA_EMAIL` and `DIGEST_JIRA_API_TOKEN`.
3. Choose who writes the summaries (automatic, first available wins):
   - **Claude:** set `ANTHROPIC_API_KEY`.
   - **Free local model:** start Ollama alongside the app (downloads ~2 GB on first run):

     ```bash
     docker compose --profile llm up -d --build
     ```

   - **Nothing:** a built-in extractive summary is used, and tickets say so.

The digest runs daily at 09:00 UTC (`DIGEST_DAILY_AT`) and once shortly after startup; **Run now**
in Settings runs it on demand. Each post is summarized once and stored. A new subscription receives
posts published after subscribing; to see a ticket right away, tick **"Also send the latest blog
post … now"** when adding the project.

## Architecture

```
web/      React + Vite + TypeScript SPA - UI only, talks to /api
  src/api/        typed API client (generated from OpenAPI), React Query hooks, error mapping
  src/pages/      Sign in, Create account, Report finding, Settings
  src/components/ project pickers, finding form, recent tickets, Jira status, API keys, digest
  src/test/       Vitest + Testing Library tests against a mocked API (msw)
server/   FastAPI backend
  app/api/       HTTP layer: request validation → service call → response
  app/services/  business logic: findings, Jira connection, API keys, blog digest
  app/jira/      Jira Cloud client (user OAuth or digest bot), ADF, error translation
  app/digest/    blog reader and summarizers (Claude, Ollama, extractive)
  app/db/        SQLAlchemy models, session, encryption-key rotation
  app/schemas/   request/response contracts shared by the UI and REST API
  app/core/      settings, encryption, CSRF, rate limiting, security headers
  scripts/       OpenAPI export (for the frontend's generated types)
  tests/         pytest, with Jira, Atlassian OAuth, the blog and the LLMs mocked
scripts/  env bootstrap (init_env.py)
```

In production, FastAPI serves the built UI, so the browser talks to a single origin: no CORS,
and session cookies stay first-party.

## Design choices

Decisions, alternatives considered, security model and known limitations are documented in
[DESIGN.md](DESIGN.md).
