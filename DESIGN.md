# Design choices

This document records the decisions behind IdentityHub's Jira integration, the alternatives that
were considered, and what a production-ready version would do differently. Setup instructions
live in the [README](README.md).

Sections marked **(planned)** describe parts that are not built yet; they are updated as each
part lands.

---

## 1. Stack and runtime

| Decision | Why |
|---|---|
| **FastAPI + Pydantic** backend | Request/response validation and OpenAPI docs come for free, which suits a product whose second consumer is a REST API for scanners and CI pipelines. |
| **React + Vite + TypeScript** frontend | Clear separation: the UI is a static app that only talks to `/api`. |
| **One origin** - FastAPI serves the built UI | No CORS configuration, and session cookies stay first-party. In development, Vite proxies `/api` to FastAPI so the browser still sees one origin. |
| **SQLite** | Zero setup for reviewers. All access goes through SQLAlchemy, so moving to Postgres is a connection-string change (plus migrations, see section 9). |
| **Docker Compose** as the primary run path | `docker compose up` is the lowest-friction way to run both halves. |
| **Locked dependencies** (`uv.lock`, `package-lock.json`) | The Docker build installs exactly the tested versions (`uv sync --locked`, `npm ci`), so the image is reproducible. |

### Libraries over custom code

Security-sensitive infrastructure uses established libraries; the code we wrote is the
business logic and the glue between them.

| Concern | Library |
|---|---|
| Accounts, password hashing, sessions | fastapi-users (database-backed session tokens) |
| Jira OAuth 2.0 (3LO) | Authlib |
| Jira REST calls | atlassian-python-api (`JiraCloud`) |
| CSRF | starlette-csrf |
| Rate limiting | limits, used as a FastAPI dependency |
| Security headers | secure |
| Encryption at rest | cryptography (`MultiFernet`) |
| Configuration | pydantic-settings |

**slowapi was replaced by `limits`.** slowapi applies limits through a decorator on our own
route functions, but the login and register routes are defined inside fastapi-users. `limits` is
the library slowapi is built on; used as a dependency, it attaches to any router.

---

## 2. Tenancy: the tenant is the user

Each user is their own tenant. Every tenant-owned table (`jira_connection`, `api_key`,
`finding`) has a non-null `user_id`, and queries always filter on the **authenticated** user's
id, never on an id taken from the request.

**Why per user rather than per organization:** each user connects Jira with their *own*
Atlassian account. Jira then enforces their real permissions: they only see projects they may
access, creating an issue fails where they lack permission, and the ticket's reporter is the
actual person. An organization-wide connection would have to re-implement Jira's permission
model, or would let users create tickets through someone else's access.

### Production: organizations with per-user data

A B2B product would add an organization above users while keeping each user's data private:

```
organization (id, name, sso_config)            <- the tenant
  └─ membership (org_id, user_id, role)
       └─ jira_connection / api_key / finding  <- carry org_id AND user_id
```

- **The org boundary is the hard wall;** inside it, a member sees only their own rows. An org
  admin role could be allowed to see all of the org's findings for auditing.
- **The database enforces membership:** a composite foreign key `(org_id, user_id)` → `membership`
  makes it impossible to store a row for a user outside that org.
- **Postgres Row-Level Security as defense in depth:** each request sets `app.org_id` and
  `app.user_id`, and policies filter on them, so a query that forgets its filter still cannot
  leak another tenant's data.
- **The tenant is resolved from the authenticated session only,** never from a header, subdomain or
  request body. A subdomain may choose which login page to show, but not what data is returned.

---

## 3. Authentication into IdentityHub

**Current:** self-service sign-up with email and password (fastapi-users).

- **Sessions are server-side.** The `identityhub_session` cookie holds only an opaque random
  token and is `HttpOnly` (unreadable by JavaScript), `SameSite=Lax`, and `Secure` when
  `APP_BASE_URL` is https. Logout deletes the session row, so a copied cookie stops working
  immediately; a stateless JWT would stay valid until it expired. Absolute lifetime: 8 hours.
- **Passwords** are hashed with Argon2, must be at least 12 characters and must not contain the
  email's local part. Registration ignores privilege fields such as `is_superuser`.
- **Self-service sign-up** was chosen so reviewers can create two accounts and check isolation.

### Production: SSO with SAML and OIDC, and SCIM provisioning

A production version would not manage passwords at all. Each customer signs in through their own
identity provider (Okta, Microsoft Entra ID, Google Workspace, ...):

- **Login via OIDC or SAML.** The customer registers IdentityHub as an application in their IdP,
  and we store that org's IdP configuration. The user enters their email, its domain identifies
  the org, and we redirect them to that org's IdP. On return we validate the assertion (OIDC:
  ID-token signature against the IdP's published keys, plus `iss`, `aud` and `nonce`; SAML: the
  signed assertion and its audience). We then find or create the user by `(issuer, subject)` and
  create our own session exactly as today.
- **Roles** come from IdP groups (e.g. `IdentityHub-Admins` → admin).
- **Provisioning and deprovisioning via SCIM.** The IdP pushes user create, update and deactivate
  events to a SCIM endpoint, so someone who leaves the company loses access immediately rather
  than when their session expires.
- **Use a broker rather than one integration per IdP.** Auth0 Organizations, WorkOS, Okta
  Customer Identity or a self-hosted Keycloak handle many customers' IdPs behind one
  integration, and usually provide SCIM and a self-service SSO setup portal.

**SSO does not give us Jira access.** Tokens from the customer's IdP are issued for IdentityHub;
Jira Cloud's REST API only accepts Atlassian-issued credentials. Even when the customer's
Atlassian organization also signs in through the same IdP, Atlassian issues its own tokens, and
there is no exchange from an IdP token to a Jira API token for a third-party app. Each user
therefore still connects Jira once with OAuth 3LO (section 4). SSO makes that a single consent
click, and the resulting token is still limited to that user's Jira permissions. The only way
to avoid a per-user OAuth step is to ship IdentityHub as an Atlassian app (Forge) installed on the
customer's site, which is a different distribution model.

---

## 4. Jira connection: OAuth 2.0 (3LO) per user **(planned)**

- **OAuth 3LO rather than API tokens:** the user never pastes a long-lived credential into our
  app, can revoke access from their Atlassian account, and we request only the scopes we need
  (`read:jira-work`, `write:jira-work`, `read:jira-user`, `offline_access`). The cost is
  reviewer setup: registering an Atlassian OAuth app takes about five minutes (see README).
- **Tokens are encrypted at rest** with `MultiFernet` at the ORM layer, so application code sees
  plaintext and the database file only holds ciphertext. They are never sent to the browser.
- **Refresh-token rotation:** Atlassian issues a new refresh token on every refresh, and the old
  one stops working. Each refresh therefore saves the new token, and refreshes are serialized
  per connection so two concurrent requests can't both use, and invalidate, the same refresh
  token.
- **Revoked access or an undecryptable token** marks the connection as needing reconnection, and
  the user sees "Reconnect Jira" instead of a server error.
- **One Jira site per user** in this POC.

---

## 5. Tickets and scope **(planned)**

- **Projects:** only projects the user can *create issues in*
  (`/project/search?action=create`), not every project they can view.
- **Recent tickets:** IdentityHub labels every issue it creates, and the list is a JQL search on
  that label, ordered by creation date, limited to 10. Jira is the source of truth, so renames,
  deletions and tickets created through the REST API all show correctly.

---

## 6. Browser security

- **CSRF:** double-submit cookie (starlette-csrf). Every state-changing browser request must
  echo the `csrftoken` cookie in an `X-CSRFToken` header. A cross-site attacker can make the
  browser *send* our cookies but cannot *read* them to build the header. Login and register are
  covered too, which prevents login-CSRF. `/api/v1/*` is exempt because it authenticates with an
  API key header, not cookies.
- **Rate limiting:** 10 requests per minute per client IP across login, logout and register,
  answered with `429` and `Retry-After`.
- **Security headers:** strict Content-Security-Policy (`script-src 'self'`, no framing),
  `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy` and `Permissions-Policy`; HSTS only when
  served over https. The `/docs` page gets a relaxed policy so Swagger UI can load from its CDN.

---

## 7. Public REST API **(planned)**

- API keys are shown once at creation and stored only as SHA-256 hashes, with a short visible
  prefix so users can tell keys apart. A key acts as its owner and uses the owner's Jira
  connection.

---

## 8. Secrets and configuration

- **Fail fast:** the app refuses to start if a required secret is missing or malformed, and the
  message names the setting and how to generate it (`scripts/init_env.py`).
- **The Jira integration is optional at startup:** without Atlassian credentials the app still
  runs, and the UI explains that the integration isn't configured.
- **Encryption key rotation:** prepend a new key to `ENCRYPTION_KEYS`, run
  `uv run python -m app.db.rotate_keys` to re-encrypt every row with it, then remove the old key.
- **Referential integrity:** SQLite foreign keys are enabled on every connection, so deleting a
  user cascades to their Jira connection, API keys and findings.

---

## 9. Known limitations and production next steps

- **SSO and provisioning** with SAML/OIDC and SCIM instead of local passwords (section 3).
- **Organizations** with membership and Row-Level Security (section 2).
- **Migrations:** the schema is created on startup (`create_all`); production would use Alembic.
- **Database:** SQLite for zero setup; production would use Postgres.
- **fastapi-users is in maintenance mode** (security fixes only). Acceptable for a POC, and moot
  once login moves to SSO.
- **No email delivery,** so no password reset or email verification.
- **Registration reveals whether an email is registered** (`REGISTER_USER_ALREADY_EXISTS`). A
  production sign-up would respond identically either way and confirm by email.
- **Rate limits are per IP and in memory** (single process). Production: a shared store (Redis)
  and an additional per-account limit against distributed credential stuffing.
- **Expired sessions are rejected but not purged;** production would run a periodic cleanup.
- **Swagger UI's "Try it out"** can't call cookie-authenticated endpoints, because it doesn't send
  the CSRF header. It is intended for the API-key REST API.
