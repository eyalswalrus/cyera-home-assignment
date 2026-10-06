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

### Libraries (no reinvented wheels)

| Concern | Library |
|---|---|
| App accounts, password hashing, sessions | fastapi-users (DB-backed session tokens) |
| Jira OAuth 2.0 (3LO) | Authlib |
| Jira REST calls | atlassian-python-api (`JiraCloud`) |
| CSRF | starlette-csrf |
| Rate limiting | limits (as a FastAPI dependency) |
| Security headers | secure |
| Encryption at rest | cryptography (`MultiFernet`) |
| Config | pydantic-settings |

## Security notes

- **Multi-tenancy - the tenant is the user.** Each user connects Jira with their *own* Atlassian
  account (OAuth 3LO), so Jira itself enforces what they may see and create: they only get
  projects they have permission for, and tickets are reported under their real identity. An
  org-level shared connection would bypass those permissions. Every tenant-owned table has a
  non-null `user_id`, and queries always filter on the *authenticated* user's id, never on an
  id from the request.
- **Jira credentials:** OAuth tokens are encrypted at rest with `MultiFernet` and are never sent
  to the browser. To rotate the key: prepend a new key to `ENCRYPTION_KEYS`, run
  `uv run python -m app.db.rotate_keys` (re-encrypts every row with the new key), then remove the old key.
- **Database integrity:** SQLite foreign keys are enabled on every connection, so deleting a user
  cascades to their Jira connection, API keys and findings.
- **Sessions:** server-side (fastapi-users `DatabaseStrategy`). The `identityhub_session`
  cookie holds only an opaque random token and is `HttpOnly` (unreadable by JavaScript),
  `SameSite=Lax`, and `Secure` when `APP_BASE_URL` is https. Logout deletes the session row, so
  a copied cookie stops working immediately. Absolute lifetime: 8 hours.
- **Passwords:** hashed by fastapi-users (Argon2 via pwdlib); minimum 12 characters and must not
  contain the email's local part. Registration ignores privilege fields (`is_superuser`, ...).
- **CSRF:** double-submit cookie (starlette-csrf). Every state-changing browser request must echo
  the `csrftoken` cookie in an `X-CSRFToken` header. Login and register are covered too (prevents
  login-CSRF). `/api/v1/*` is exempt because it authenticates with an API key header, not
  cookies.
- **Rate limiting:** 10 requests/minute per client IP across login, logout and register;
  responds `429` with `Retry-After`.
- **Security headers:** strict CSP (`script-src 'self'`, no framing), `X-Frame-Options: DENY`,
  `nosniff`, `Referrer-Policy`, `Permissions-Policy`; HSTS only when served over https. The
  `/docs` page gets a relaxed CSP so Swagger UI can load from its CDN.
- **API keys:** _TBD_

## Scope decisions & assumptions

_TBD_

## Known limitations / production next steps

- Schema is created on startup (`create_all`); production would use Alembic migrations.
- SQLite is used for zero-setup; production would use Postgres.
- fastapi-users is in maintenance mode (security fixes only) - acceptable for a POC.
- No email delivery, so no password reset or email verification.
- Registration reveals whether an email is already registered (`REGISTER_USER_ALREADY_EXISTS`);
  a production sign-up would answer identically either way and confirm by email.
- Rate limits are per IP and in memory (single process). Production: a shared store (Redis) and
  an additional per-account limit against distributed credential stuffing.
- Expired session rows are rejected but not purged; production would run a periodic cleanup.
- Swagger UI's "Try it out" can't call cookie-authenticated endpoints, because it doesn't send
  the CSRF header. It is intended for the API-key REST API.
