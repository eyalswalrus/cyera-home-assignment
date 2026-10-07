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
| 10 most recent tickets created from the app | **Recent tickets** page, each opening in Jira in a new tab |
| REST API with an API key | `POST /api/v1/findings`; keys with expiry, per-project permissions and notes under Settings → API keys (DESIGN §8) |
| Bonus: NHI Blog Digest | Settings → NHI Blog Digest; a scheduled job files each new Oasis Security blog post with an AI summary (DESIGN §9) |

## Quick start

### 1. Create the Atlassian OAuth app (~5 minutes)

You need a Jira Cloud site (a free one from [atlassian.com](https://www.atlassian.com/software/jira/free)
works) and an OAuth 2.0 app that IdentityHub signs in through:

1. Open the [Atlassian developer console](https://developer.atlassian.com/console/myapps/) →
   **Create** → **OAuth 2.0 integration**, give it a name (e.g. "IdentityHub local") and choose the
   **resource-level grant** (consent is limited to the Jira site you pick).
2. **Permissions** → **Jira API** → **Add**, then **Configure** and add the classic scopes
   `read:jira-work`, `write:jira-work` and `read:jira-user`.
   (`offline_access`, for refresh tokens, is requested at sign-in and needs no setup.)
3. **Authorization** → **OAuth 2.0 (3LO)** → set the callback URL to
   `http://localhost:8000/api/jira/callback`.
4. **Settings** → copy the **Client ID** and **Secret** for step 2 below.

By default an Atlassian OAuth app can only be authorized by its owner. To connect a second
Atlassian account (for example to check that two users see different projects), set
**Distribution** → **Sharing** in the console.

#### Why each scope

| Scope | Jira calls | Used for |
|---|---|---|
| `read:jira-work` | `GET /project/search?action=create`, `GET /issue/createmeta/{project}/issuetypes`, `GET /search/jql` | The project picker (only projects you can create issues in), choosing the issue type, and the 10 recent tickets list |
| `write:jira-work` | `POST /issue` | Creating finding tickets, the only write IdentityHub makes |
| `read:jira-user` | `GET /myself` | Showing which Atlassian account is connected ("Connected to *acme* as *Alice*") |
| `offline_access` | none; it makes Atlassian issue a refresh token | Access tokens last about an hour. The REST API and the digest act on your behalf when you aren't in the browser, so the token must be renewable without you. |

Jira also checks your own permissions on every call, so these scopes never let IdentityHub do
more than you can. Nothing edits, comments on, deletes or assigns issues, so those scopes aren't
requested. These are Atlassian's *classic* scopes; the narrower *granular* scopes are a production
refinement (see [DESIGN.md](DESIGN.md) §4).

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
(cd server && uv sync && ENVIRONMENT=development uv run uvicorn app.main:app --reload)

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

The API reference (authentication, errors, rate limits, and *Try it out*) is at
http://localhost:8000/api/v1/docs, also linked from **Settings → API keys**. The full schema,
including the internal endpoints the web UI uses, is served at http://localhost:8000/docs only
with `ENVIRONMENT=development` (as in the local development command above).

## Bonus: NHI Blog Digest

Each new post on the [Oasis Security blog](https://www.oasis.security/blog) is summarized and
filed as a Jira ticket in the projects you choose. It needs **no extra Jira setup**: tickets are
filed with your own Jira connection (the ticket says so), and only in projects you can create
issues in.

### Try it

After the [Quick start](#quick-start) (Jira connected):

1. *Optional, do this first:* pick who writes the summaries (next section). Without any setup a
   built-in summary is used. Each post is summarized **once and stored**, so a post summarized
   before you add Claude or the local model keeps its first summary.
2. Open **Settings** (account menu, top right) → **NHI Blog Digest**.
3. Under **Projects that receive the digest**, choose one or more projects and click
   **Save projects**.
4. Click **Send latest post** next to a project. A notification links to the new ticket (its title is
   "NHI Blog Digest: *post title*"). It also appears on the **Recent tickets** page.

From then on it runs by itself: daily at 09:00 UTC, plus once shortly after the server starts.
Each run files posts published since the project last received one, one ticket per post per
project. A new subscription starts with posts published after subscribing; **Send latest post**
is how you get one right away.

### Who writes the summaries

Checked in this order on each run; the first one available is used:

| | Setup | Notes |
|---|---|---|
| **1. Claude** | Add `ANTHROPIC_API_KEY=sk-ant-...` to `.env`, then `docker compose up -d` | Best summaries; needs an Anthropic API key |
| **2. Local model** (Ollama, free) | `docker compose --profile llm up -d --build`, then wait for the model download (below) | ~2 GB download once; about a minute per summary on a laptop CPU |
| **3. Built-in** | Nothing | Picks the post's key sentences; not an LLM, and the ticket says so |

With the local model, wait until the download finishes before step 4. Until then, the built-in
summary is used. Check that it's ready with:

```bash
docker compose exec ollama ollama list
```

It's ready when the list shows `llama3.2:3b`. (`docker compose logs -f ollama` shows the
download's progress.)

To see which one summarized a post, look at the last line of its ticket, e.g. "Summary: local
model llama3.2:3b (Ollama)".

**Running the server outside Docker** (local development): start only the model with
`docker compose --profile llm up -d ollama`. The server finds it at `http://localhost:11434`.

### Optional settings (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | Enables Claude |
| `LLM_PROVIDER` | `auto` | `auto` (order above), or force one: `anthropic`, `ollama`, `extractive` |
| `OLLAMA_MODEL` | `llama3.2:3b` | Model the local service downloads and uses |
| `DIGEST_DAILY_AT` | `09:00` | Daily run time, in UTC |
| `DIGEST_JITTER_MINUTES` | `30` | Each run starts at a random time up to this long after `DIGEST_DAILY_AT` |

After changing `.env`, apply it with `docker compose up -d`. Add `--profile llm` if you use the
local model.

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
  app/jira/      Jira Cloud client (user OAuth), ADF, error translation
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
