# Design choices

This document records the decisions behind IdentityHub's Jira integration, the alternatives that
were considered, and what a production-ready version would do differently. Setup instructions
live in the [README](README.md).

---

## 1. Stack and runtime

| Decision | Why |
|---|---|
| **FastAPI + Pydantic** backend | Request/response validation and OpenAPI docs come for free, which suits a product whose second consumer is a REST API for scanners and CI pipelines. |
| **React + Vite + TypeScript** frontend | Clear separation: the UI is a static app that only talks to `/api`. |
| **One origin** - FastAPI serves the built UI | No CORS configuration, and session cookies stay first-party. In development, Vite proxies `/api` to FastAPI so the browser still sees one origin. |
| **SQLite** | Zero setup for reviewers. All access goes through SQLAlchemy, so moving to Postgres is a connection-string change (plus migrations, see section 12). |
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
| Jira retries and caches | tenacity (burst-limit retries), cachetools (TTL caches) |
| Security headers | secure |
| Encryption at rest | cryptography (`MultiFernet`) |
| Configuration | pydantic-settings |
| Blog parsing | BeautifulSoup (links, JSON-LD), trafilatura (article text) |
| Summaries | anthropic (Claude), ollama (local model) |
| HTTP | httpx2, the maintained fork of httpx (see below) |

**One HTTP stack: httpx2.** The Anthropic SDK (1.x) and Authlib use `httpx2`, the maintained,
API-compatible fork of `httpx`. `app/__init__.py` calls `httpx2.alias_httpx()` before anything else
imports `httpx`, so our own code, the `ollama` client and the test mocks (respx) all share it. Two
stacks side by side would mean, for example, that `except httpx.HTTPError` silently misses an
error raised inside Authlib.

**slowapi was replaced by `limits`.** slowapi applies limits through a decorator on our own
route functions, but the login and register routes are defined inside fastapi-users. `limits` is
the library slowapi is built on; used as a dependency, it attaches to any router.

---

## 2. Tenancy: the tenant is the user

Each user is their own tenant. Every tenant-owned table (`jira_connection`, `api_key`,
`finding`, `digest_subscription`) has a non-null `user_id`, and queries always filter on the
**authenticated** user's id, never on an id taken from the request. The blog digest's posts and
deliveries are deliberately *not* per user: a digest ticket belongs to a Jira project, shared by
everyone subscribed to it (section 9).

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

### How SSO and per-user Jira permissions work together

The two are separate layers that combine:

| Layer | Answers | Mechanism | Token issued by |
|---|---|---|---|
| Sign-in to IdentityHub | *Who is this person?* | SSO (OIDC / SAML) | The customer's IdP |
| Access to Jira | *What may they do in Jira?* | Per-user OAuth 3LO | Atlassian |

1. The user signs in to IdentityHub through their IdP.
2. The first time, they click "Connect Jira". The customer's Atlassian organization usually signs
   in through the same IdP, so the redirect goes IdentityHub → Atlassian → IdP (already signed in)
   → back. The user sees only a one-time consent screen, with no password prompt.
3. IdentityHub stores the token encrypted and refreshes it in the background (`offline_access`).
4. Every Jira call uses that user's token, so Jira checks their permissions at call time.
   IdentityHub never caches permissions, so changes made in Jira apply on the next request.

To make this robust in production:

- **Check that the Jira account belongs to the person.** After the OAuth callback, call Jira's
  `/myself` and check that the Atlassian account matches the SSO identity (same email, or the
  company's verified domain). Otherwise a user could connect a personal or someone else's
  Atlassian account. Email visibility depends on the Atlassian profile's privacy settings;
  company-managed accounts usually expose it, but this needs verifying per customer.
- **Offboarding comes from both sides.** When the IdP deactivates a user, SCIM tells IdentityHub to
  delete their sessions, Jira token and API keys. The IdP also deprovisions their Atlassian
  account, so any stored token stops refreshing even if our cleanup missed something.
- **Automation uses a service identity, not a person.** A scanner calling the REST API with a
  person's API key acts as that person and breaks when they leave. In production, automated
  sources would use an organization-level service account: a dedicated Atlassian user whose
  permissions cover only the projects scanners may create tickets in. This is the one case where
  an org-level Jira identity is appropriate, and it is what the blog digest would use in
  production (section 9).

---

## 4. Jira connection: OAuth 2.0 (3LO) per user

**Why OAuth rather than API tokens:** the user never pastes a long-lived credential into our app,
can revoke access from their Atlassian account, and we request only the scopes we need:
`read:jira-work`, `write:jira-work`, `read:jira-user`, and `offline_access` for a refresh token.
The cost is reviewer setup: registering an Atlassian OAuth app takes about five minutes (README).
That is a testing convenience only. Atlassian requires a real integration to ship **one**
distributable OAuth app (shared with all customers, ideally listed on the Marketplace) rather than
asking each customer to create their own; a production IdentityHub would own that app and its
secret.

### The flow

1. **`GET /api/jira/connect`** (a browser navigation): Authlib builds the Atlassian authorize URL
   with a random `state`, stored in a short-lived signed cookie (`identityhub_oauth`, 10 minutes,
   `SameSite=Lax` so it survives the redirect back). We also record *which IdentityHub user*
   started the flow.
2. **The user consents at Atlassian**, which redirects to **`GET /api/jira/callback`**. There:
   - `state` must match the stored value, which defeats forged or replayed callbacks;
   - the logged-in user must be the one who started the flow, so one person's Jira grant can never
     be attached to another account (for example if the browser's user changed mid-flow);
   - Authlib exchanges the code for tokens.
3. **Site discovery:** `accessible-resources` lists the sites the grant covers, keeping only those
   with Jira write access (the same endpoint lists Confluence sites). One site connects
   immediately. Several sites put the connection in a `needs_site` state, and the user picks one
   (`GET /api/jira/sites`, `PUT /api/jira/connection/site`); a site outside the grant is rejected.
   No sites gives a clear "your account has no Jira site" message.
4. **Identity:** we call Jira's `/myself` and store the account id and display name, so the UI can
   show "Connected to *acme* as *Alice*".
5. **The callback always redirects back to the UI** with either `?jira=connected|choose_site` or
   `?jira_error=<code>`. Only fixed codes travel in the URL, never free text or a caller-supplied
   destination, so the redirect can't be used for phishing or open redirects.

### Tokens

- **Encrypted at rest** with `MultiFernet` at the ORM layer, so application code sees plaintext and
  the database file only holds ciphertext. They are never sent to the browser:
  `GET /api/jira/connection` returns only status, site and account name.
- **Refresh 60 seconds before expiry.** Atlassian *rotates* refresh tokens: every refresh returns a
  new one and invalidates the old. The new token is saved immediately, and refreshes are
  serialized per connection with a lock. Without it, two concurrent requests could both present
  the same refresh token; the second would fail and log the user out of Jira. A test runs three
  concurrent requests against an expired token and asserts exactly one refresh.
- **Failure modes are kept distinct,** because they need different responses:

  | What happened | Connection becomes | User sees |
  |---|---|---|
  | Refresh token revoked or expired (`invalid_grant`) | `needs_reauth` | "Reconnect Jira" |
  | Token can't be decrypted (encryption key lost) | `needs_reauth` | "Reconnect Jira" |
  | Atlassian unreachable | unchanged | "Jira couldn't be reached, try again" (502) |

  A temporary outage must not force a user to reconnect.
- **Disconnect** deletes our copy of the tokens. Atlassian has no token-revocation endpoint for
  3LO apps; users can also remove the app under *Connected apps* in their Atlassian account.

### Errors

Jira-related failures are raised as typed exceptions (`app/jira/errors.py`), each with an HTTP
status, a stable `code` and a user-facing message. One exception handler turns them into
`{"detail": "...", "code": "..."}`, so the UI and API clients get the same shape everywhere.

### Scope of this POC

- **One Jira site per user.**
- **Classic scopes.** `write:jira-work` also permits editing and deleting issues, comments and
  attachments, which IdentityHub never does. Atlassian's granular scopes (e.g. `write:issue:jira`,
  `read:project:jira`, `read:issue-details:jira`) would match our calls more closely; switching is a
  production step to verify against a real site, since some endpoints need several granular
  scopes. Why each classic scope is needed is in the README.
- **The refresh lock is per process.** Several workers or replicas would need a distributed lock
  (e.g. a Postgres advisory lock or Redis).
- **To verify against real Atlassian:** the token request sends client credentials in a form body
  (`client_secret_post`). Atlassian documents a JSON body; the tests mock this exchange, so it is
  confirmed only once real credentials are configured.
- **HTTP client:** the whole app runs on `httpx2` (section 1), which Authlib uses natively.

---

## 5. Tickets: what we support and why

### Projects

- **Only projects the user can create issues in** (`/project/search?action=create`), not every
  project they can browse, so the picker never offers a project that would fail on submit.
- **Searchable, 20 at a time.** The picker filters by name or key on the server; users with many
  projects type to narrow the list rather than scrolling through hundreds. Each page costs Jira
  rate-limit points per project returned, so it is small and cached (section 11).

### Fields

| Field | Required | Goes to Jira as | Why |
|---|---|---|---|
| Project | yes | `project` | Picked from the list above. Keys are validated and upper-cased. |
| Title | yes | `summary` | 1–255 characters, single line (Jira's own limits). |
| Description | no | `description` | Free text, up to 30,000 characters. |
| Finding type | no | Description header + label `nhi-<type>` | Stale identity, over-privileged, expiring credential, exposed secret, other: the problems named in the brief. |
| Severity | no | Description header + label `severity-<level>` | Critical / high / medium / low. |
| Identity | no | Description header | The affected service account, key or principal. |

**Why NHI details go into the description and labels rather than Jira fields:** Jira projects
differ. *Priority* has per-project schemes, *Components* must already exist, and custom fields
have per-site ids. Writing to them would fail on many projects. Description and labels exist
everywhere, labels are searchable in JQL (e.g. `labels = severity-critical`), and the result reads
well in Jira.

**Issue type:** the first of Task, Bug or Story the project offers, otherwise any non-subtask
type. A project with no usable type gets a clear message. Type names come back in the user's
Jira language, so on a non-English site the fallback usually applies.

**Out of scope:** projects whose create screen has *required custom fields*. Jira rejects those
tickets and the user sees Jira's own reason, e.g. "Jira rejected the request: Team is required."
Supporting them would mean reading each project's create metadata and rendering dynamic form
fields.

**Description format:** Jira's v3 API needs Atlassian Document Format (ADF). A small converter
(`app/jira/adf.py`) turns plain text into paragraphs and line breaks and never interprets markup,
so `svc_deploy_prod` stays literal. The v2 API would take plain text but reads it as wiki markup,
where underscores can become italics. No mainstream Python ADF library exists, and the converter
is about 20 lines.

### Recent tickets

- **Every ticket IdentityHub creates carries the label `identityhub`.** "Recent tickets" is the JQL
  search `project = "<KEY>" AND labels = "identityhub" ORDER BY created DESC`, limited to 10.
- **Jira is the source of truth:** renamed issues show their current title, and tickets created
  through the REST API or by teammates appear too.
- **Deleted tickets are flagged, not dropped.** A finding silently disappearing is bad for a
  security tool, so the user's *own* recent tickets that Jira no longer returns stay in the list,
  greyed out as "No longer in Jira", without a link. Jira can't tell us *why*: deleted, moved to
  another project, or access lost. They are found by comparing the search with the user's records
  in the `finding` table, checked with one bulk fetch (`POST /issue/bulkfetch`); a `key in (...)`
  JQL query would fail outright on the first deleted key. The same check covers a ticket created
  seconds ago that Jira's search index hasn't caught up with, which is shown normally. Only the
  user's own records are used, never other tenants'.
- **It runs as the user,** so it shows only issues they are allowed to see. That includes tickets
  that *other* IdentityHub users created in the same project, which is what "created from this
  app" means. Showing only the user's own tickets would be a one-line change
  (`AND reporter = currentUser()`).
- **Caveat:** anyone can add the `identityhub` label to an issue by hand. Atlassian's
  non-editable alternative, indexed issue properties, is only available to Forge and Connect apps.
- **JQL injection:** the project key is validated against `^[A-Z][A-Z0-9_]{1,19}$` before it is
  placed in the query.
- **The `finding` table** records every ticket IdentityHub creates: who, when, from the UI or the
  API (and with which API key), and which issue. It is the audit trail Jira can't provide (Jira
  doesn't know a ticket came from IdentityHub, or through which key; if a key leaks, this is how to
  find what it did), and it powers the deleted-ticket flags above. It is kept for
  `FINDING_RETENTION_DAYS` (section 10).

### Jira errors users will actually see

| Jira says | User sees | HTTP |
|---|---|---|
| 400 (e.g. required field) | "Jira rejected the request: *Jira's reason*" | 422 |
| 401 | "Reconnect Jira" (connection marked `needs_reauth`) | 409 |
| 403 | "Your Jira account doesn't have permission to create issues in SEC." | 403 |
| 404 | "Project SEC wasn't found, or your Jira account can't access it." | 404 |
| 429, short burst limit | Nothing, if a quick retry succeeds; otherwise "Jira is receiving too many requests right now…" | 429 + `Retry-After` |
| 429, hourly quota used up | "IdentityHub has used its hourly Jira API quota. It resets in about N minutes…" | 429 + `Retry-After` |
| 5xx / network | "Jira couldn't be reached. Please try again in a moment." | 502 |

Input problems (missing title, unknown fields, invalid project key) are rejected with a 422
*before* Jira is called.

---

## 6. User interface

**Stack:** React + TypeScript, Mantine components, TanStack Query for server state,
react-hook-form + zod for forms, React Router.

### Pages

| Page | What it does |
|---|---|
| Sign in / Create account | Registration signs the user straight in. Password rules are shown up front, and server-side rejections appear on the password field. |
| Report finding | Searchable project picker and the finding form. |
| Recent tickets | Its own project picker and the 10 newest tickets IdentityHub created there (findings and digests, from the UI and the API). A separate page, so looking up tickets doesn't mean going through the filing form. |
| Settings | **Jira connection:** connected site and account, reconnect or switch account, disconnect (with confirmation), choose a site, and the outcome of the OAuth redirect. **NHI Blog Digest:** recipient projects, latest ticket or problem per project, and *Send latest post* per project. **API keys:** create (permissions locked once created), one-time reveal, notes, revoke. |

### Interaction decisions

- **The UI follows the Jira connection state.** Each state gets one clear next step instead of a
  form that would fail: not configured on the server, *Connect Jira*, *Choose a site*,
  *Reconnect Jira*, or the form. If Jira rejects the token mid-request, the banner switches to
  *Reconnect Jira* without a reload.
- **Project picker:** searches Jira as you type (debounced), lists only projects you can create
  issues in, and remembers your last project per user and Jira site in this browser. The report
  and recent-tickets pages share that memory, so after filing in SEC, *Recent tickets* opens on SEC.
- **The form** is disabled until a project is chosen, validates instantly with the same limits as
  the server (the server stays the authority), and disables the submit button while sending so a
  double click can't create two tickets. On success it shows the new key with an *Open in Jira*
  link, clears the fields, keeps the project, and refreshes the recent-tickets list. On failure it keeps
  everything the user typed and shows the server's message, e.g. "Your Jira account doesn't have
  permission to create issues in SEC."
- **Recent tickets** open in a new tab (`rel="noopener noreferrer"`), show relative time with the
  exact time on hover, and give the title its own line, since that's what people scan for.
- **One error shape:** every failure becomes an `ApiError` with a readable message. That covers
  server `detail`/`code`, fastapi-users codes such as `LOGIN_BAD_CREDENTIALS`, 422 field errors
  mapped onto inputs, and network failures. 4xx responses aren't retried; network errors and 5xx
  are retried twice.
- **Light and dark mode** follow the operating system. Layouts work at phone width without
  horizontal scrolling.

### Wiring

- **Typed API client:** TypeScript types are generated from FastAPI's OpenAPI schema
  (`npm run gen:api`, openapi-typescript) and used through `openapi-fetch`, so a backend contract
  change shows up as a frontend type error. openapi-typescript is run via `npx` with its own
  TypeScript 5, because it doesn't support the project's TypeScript 6 yet; it is a code generator
  and never ships in the app.
- **CSRF:** a client middleware reads the `csrftoken` cookie and sends it as `X-CSRFToken` on every
  write.
- **Session:** `/api/auth/me` decides whether to show the app or the login page. After login,
  users land on the page they originally asked for, and only same-app paths are followed.
  Logging out clears all cached data, so the next user in the same browser starts clean.
- **Tests:** Vitest + Testing Library render the real routes against a mocked API (msw): auth
  errors, each connection state, the OAuth error banner, creating a ticket (payload and CSRF
  header), client-side validation, server errors, the recent list's links, API-key creation,
  reveal, notes and revocation, and the digest settings.

---

## 7. Browser security

- **CSRF:** double-submit cookie (starlette-csrf). Every state-changing browser request must
  echo the `csrftoken` cookie in an `X-CSRFToken` header. A cross-site attacker can make the
  browser *send* our cookies but cannot *read* them to build the header. Login and register are
  covered too, which prevents login-CSRF. `/api/v1/*` is exempt because it authenticates with an
  API key header, not cookies.
- **Rate limiting:** 10 requests per minute per client IP across login, logout and register;
  60 per minute per API key and 120 per IP on `/api/v1`; 5 per minute on the digest's *Send latest post*.
  All answered with `429` and `Retry-After`.
- **Security headers:** strict Content-Security-Policy (`script-src 'self'`, no framing),
  `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy` and `Permissions-Policy`; HSTS only when
  served over https. The docs pages (`/docs`, `/api/v1/docs`) get a relaxed policy so Swagger UI
  can load from its CDN.

---

## 8. Public REST API and API keys

### The endpoint

`POST /api/v1/findings` takes the same body as the UI (`project_key`, `summary`, optional
`description`, `finding_type`, `severity`, `identity_name`), so the UI and API share one
validated contract and one service.

- **Authentication:** `Authorization: Bearer ihub_...`. Only API keys are accepted, never the
  browser session cookie, which is also why `/api/v1` is exempt from CSRF.
- **Versioned under `/api/v1`**, so the contract can change without breaking existing scanners.
- **`201 Created`** with a `Location` header pointing to the new Jira issue, and
  `{key, url, summary}` in the body.
- **Its own reference** at `/api/v1/docs` (schema at `/api/v1/openapi.json`), which is what the UI
  links to. It contains only the public API, with an introduction to authentication, key
  permissions, the error format and rate limits, the bearer scheme and every error status.
  Swagger's *Authorize* and *Try it out* work, because this API doesn't use cookies or CSRF. The
  internal endpoints the web UI uses aren't part of that contract, so they're only in the full
  schema at `/docs`; production would serve that one only in development.

| Status | When | `code` |
|---|---|---|
| 401 | No key; malformed, unknown, revoked or expired key; owner deactivated. Includes `WWW-Authenticate`. | `api_key_missing`, `api_key_invalid`, `api_key_revoked`, `api_key_expired` |
| 403 | The key lacks the permission or the project; or the owner's Jira account lacks permission | `api_key_scope_missing`, `api_key_project_forbidden`, `jira_forbidden` |
| 404 | Project not found in Jira, or not visible to the key's owner | `jira_not_found` |
| 409 | The owner's Jira connection needs attention (not connected, reconnect) | `jira_not_connected`, `jira_reauth_required` |
| 422 | Invalid body (field-level details), or Jira rejected the ticket | (FastAPI validation), `jira_validation` |
| 429 | Rate limited (60/min per key, 120/min per IP); includes `Retry-After` | `rate_limited` |
| 502 | Jira unavailable | `jira_unavailable` |

Checks run cheapest first: authenticate the key, then rate-limit it, then check scope and
project, and only then call Jira. A refused request never reaches Jira.

### API keys

- **Format and storage:** `ihub_` plus 256 random bits. Only a SHA-256 hash is stored. A fast hash
  is fine here, unlike for passwords, because the input is already high-entropy and can't be
  brute-forced. Lookup is one indexed query.
- **Shown once,** at creation, with a copy button and a ready-made `curl` command. Afterwards the
  UI shows only the first characters, `ihub_ax3K************`: 4 random characters (about 24 of
  256 bits) are enough to tell keys apart, and the fixed-length mask doesn't reveal the key's size.
- **Each key acts as its owner,** through the owner's Jira connection, so Jira permissions still
  apply on top of the key's own.
- **Lifecycle:** keys always expire (7, 30, 90, 180 or 365 days; no "never") so a key leaked in a
  CI log stops working on its own. They can be revoked at any time and show when they were last
  used. Each user can have at most 20 active keys.
- **Notes** record what a key is used for and who owns it, and are the only thing that can be
  edited after creation.

### Permissions: granular, immutable, and built to evolve

Each key carries one permissions document, fixed at creation:

```json
{"version": 1, "scopes": ["findings:create"], "projects": ["SEC", "PLAT"]}
```

- **Granular:** *scopes* say which actions a key may perform; today there is one,
  `findings:create`. *Projects* say where. At creation the server checks with Jira that the owner
  can create issues in every listed project, so a key can never be granted more than its owner
  has. "Board" in the brief maps to the Jira *project*: tickets live in projects, and boards are
  views over them.
- **Immutable:** the only update endpoint (`PATCH /api/api-keys/{id}`) accepts `notes` and nothing
  else. Sending `permissions`, `expires_in_days` or `name` returns a 422 rather than being
  silently ignored. To change what a key can do, create a new key and revoke the old one. This
  means a key's power can't quietly grow after it has been handed to a CI system, and the stored
  document is an exact audit record.
- **New options don't change existing keys.** The rules (also in `app/schemas/api_keys.py`):
  - A new **action** becomes a new scope. Existing keys don't hold it, so they can't do it.
  - A new **restriction** (e.g. an IP allowlist or a maximum severity) becomes an optional field
    whose absence means "not restricted in this dimension". Existing keys behave exactly as
    before.
  - Changing the *meaning* of an existing field requires bumping `version` and handling both.
- **Fail closed:** permissions are re-validated on every request. A document this server doesn't
  fully understand, for example one with a field added by a newer release that was then rolled
  back, is refused (401 `api_key_permissions_unreadable`) rather than having the restriction
  silently ignored. Such a key still appears in the list, so it can be seen and revoked.

### With several users per tenant

Today the tenant is the user, so the owner already has full access to everything in their tenant
and a key's effective permission is *(the key's scopes and projects) ∩ (the owner's Jira
permissions)*.

With organizations (section 2), each user would hold a role inside the org, and a key would also
be capped by its creator's role. Its effective permission becomes *(key) ∩ (creator's role in the
org) ∩ (creator's Jira permissions)*, so no key can do more than the person who created it:

- Creating a key with a scope or project outside the creator's role is refused, just as projects
  outside their Jira permissions are refused today.
- The role is evaluated on **every request**, not copied into the key, so demoting or removing the
  user (for example through SCIM) immediately narrows or disables their keys.
- Org admins would see and revoke all keys in the org, and could set policy such as a maximum
  lifetime or allowed scopes.
- Keys for unattended systems would belong to a **service account** with its own role, so they
  don't stop working when an employee leaves (section 3).

### Off-the-shelf alternatives considered

- **Policy engines** (pycasbin in-process; Cerbos or OPA as services) evaluate "can X do Y on Z"
  from external policy files. Too much for one action plus a project allowlist, but the natural
  next step once orgs and roles exist; the permissions document above maps directly onto their
  policy inputs.
- **Hosted API-key management** (e.g. Unkey, or an API gateway) provides issuing, hashing, expiry,
  rate limits and per-key permissions as a service, at the cost of an external dependency that
  breaks "runs with one command".
- **OAuth2 client credentials** from the customer's IdP (short-lived tokens with scopes) is the
  standard machine-to-machine pattern and the production direction once SSO exists. The brief asks
  specifically for an API key, so this POC implements keys.

---

## 9. Bonus: NHI Blog Digest

Each new post on oasis.security/blog is summarized and filed as a Jira ticket, with the post's
title and the summary, in every project users have subscribed to the digest.

### Who does what

| | Configured by | Where |
|---|---|---|
| The **summarizer** (Claude key / local model) and the schedule | The deployer, once | `.env`, like the Atlassian OAuth secrets |
| **Which projects** receive the digest | Each user, for projects they work in | Settings → NHI Blog Digest |

The automation itself has no UI, as the brief specifies: it is a scheduled job inside the server.
The Settings section shows a subscriber only what concerns them: their projects, each project's
latest digest ticket or problem, and a **Send latest post** button per project. How the server
runs the digest (summarizer, schedule, last run) is in the API (`GET /api/digest`) and the logs,
not in the UI.

### Who files the tickets: the subscriber's own Jira connection

- **Each ticket is created with a subscriber's own OAuth connection,** the project's earliest
  subscriber who can still create issues there. Jira enforces a real user's permissions, so the
  digest can never post where its subscribers couldn't. The ticket footer names whose connection
  filed it ("Filed by IdentityHub's NHI Blog Digest with *Alice*'s Jira connection").
- **Access is re-checked:** when subscribing (the picker only offers projects the user can create
  issues in, and the server re-validates), and on every run. If no subscriber can still create
  issues in a project, nothing is filed and the subscription shows why.
- **Projects are identified by site and key.** Subscribers may be connected to different Jira
  sites, and project keys are only unique within a site, so subscriptions and deliveries record
  the site (`site_url`) as well as the key.
- **Why not an app identity here.** A 3LO OAuth app always acts as the user who authorized it;
  there is no "app user" to file as (only Forge apps can act `asApp()`, section 3). A dedicated
  bot account would need a second Atlassian user and an API token from every reviewer, so this POC
  uses the subscribers' connections. Production would not (*In a production setting* below).

### The run

- **When:** daily at a fixed time, `DIGEST_DAILY_AT` (09:00 UTC by default), so restarts don't
  shift the schedule. A catch-up run also happens shortly after the server starts, in case it was
  down at the scheduled time; it is cheap because filed posts and stored summaries are reused.
  There is no endpoint to run it for everyone on demand: that would let any user trigger work for
  all subscribers. Users get *Send latest post* for their own projects instead.
- **Which posts:** the blog has no RSS feed, and its index pins an older featured post at the top.
  The digest takes the first 8 post links and reads each post's schema.org `BlogPosting` JSON-LD
  for its `datePublished` (the post pages show no visible date of their own); `trafilatura`
  extracts the article text.
- **What each project is due (catch-up and fresh start).** Every project has a *watermark*:
  - the publish date of the newest post already filed there, or
  - when its current subscriptions began, if that is later. That is the **fresh start**: a new
    subscription receives posts published after subscribing, not the existing backlog.
  - **Send latest post** (a button per subscribed project) files the newest post in that project
    right away, together with any earlier posts still due there, so none is skipped; if it is
    already there, it says so. It shares the run's lock, so it can't race the scheduled run into
    filing a post twice.

  Each run files the posts published after the watermark that aren't in the project yet, **oldest
  first**, so a day with two new posts files both. At most 5 per project per run, so a long outage
  can't flood a project. If a post fails, that project stops there and retries next run, so the
  order is kept.
- **Summarized once, stored.** Each post's summary is saved in `digest_post` the moment it is
  generated, and every project and every later run reuses it, so the model is never asked about
  the same post twice. A failed summary isn't saved, so it is retried.
- **Exactly once per (post, project).** A unique `digest_delivery` row is written as each ticket
  is created, so two subscribers of the same project get one ticket, and re-runs or crashes
  mid-run can't create duplicates. There is no per-user record because tickets go to projects, not
  people; the "last ticket" a user sees is the project's latest delivery.
- **Tickets** carry the labels `identityhub` and `nhi-blog-digest` (so they also appear in the
  project's recent tickets list, alongside findings), a link to the post, its publish date, and which summarizer wrote
  the summary.

### Summaries: Claude, a free local model, or no LLM

Reviewers may not have an Anthropic API key, so the summarizer is chosen per run (`LLM_PROVIDER`,
default `auto`), with the first available option winning:

1. **Claude** (Opus 5.5 via the official SDK), when `ANTHROPIC_API_KEY` is set. Medium effort,
   server-side refusal fallback; a refusal, empty or truncated answer is an error, not a ticket.
2. **A local model via Ollama**, free and offline: `docker compose --profile llm up` starts it and
   downloads `llama3.2:3b` (about 2 GB) on first start. Used when reachable and the model is
   present. Measured on an Apple-silicon laptop's CPU in Docker: about a minute per summary, fine
   for a daily background job, with summaries that follow the requested format and stay on the
   facts.
3. **Extractive**, built in: the most representative sentences of the article (word-frequency
   scoring). Not an LLM, so it is only the fallback, and tickets say so. Written in ~30 lines
   rather than pulling in `sumy`, which needs NLTK's tokenizer data downloaded at build or first
   run, too much for a last resort.

Free hosted tiers (Gemini, Groq, OpenRouter, ...) were considered but still need a sign-up and
key, and their terms change often.

The `ollama` Compose service publishes its port on `127.0.0.1` only, so a server run outside
Docker can use the model too (`docker compose --profile llm up -d ollama`).

**The blog is untrusted input.** The article is fenced in `<article>` tags, and the prompt says to
treat it as content, never as instructions. The model has no tools and its output only becomes
ticket text, so a prompt injection in a post could at worst produce a misleading summary.

### In a production setting

Filing with a subscriber's connection, a laptop CPU model and extractive summaries keep this POC
runnable with one Atlassian account and no paid keys. A production deployment would change them:

- **File as the app, never as a person.** Automation reported as a person is misleading in Jira,
  and it stops working when that person leaves or disconnects. The identity would be one of:
  - a **Forge app** calling Jira `asApp()`, the only true app identity: installed by each customer's
    site admin, with its permissions defined by the app's scopes;
  - an **Atlassian service account** from Atlassian Administration, where available on the plan,
    with a scoped API token or OAuth client credentials, granted *Create Issues* only on the
    digest projects.

  The access rules stay: a user may only subscribe projects they can create issues in themselves,
  re-checked on every run, so the app identity never lets anyone post where they couldn't.
- **Per customer, not per deployment.** With organizations (section 2), each customer org has its
  own app installation or service account and site. Credentials live in a secrets manager with
  rotation, not in `.env`.
- **A hosted model behind the company's AI gateway**, not a laptop CPU model: e.g. Claude directly
  or through Bedrock or Vertex, with the data-handling terms the company already has, plus cost and
  rate controls. If self-hosting is required, a GPU inference service (e.g. vLLM) behind the same
  gateway. The extractive summary stays only as a degradation path, and summary quality is checked
  with a small evaluation set before changing models or prompts.
- **A scheduler outside the web process:** a cron job or worker (e.g. a Kubernetes CronJob) with a
  distributed lock, retries, and alerting on failed runs, instead of a task inside one app replica.
- **A feed contract for the source.** Scraping HTML and JSON-LD is brittle; production would ask
  for RSS or an API, and alert when parsing finds no posts.

---

## 10. Secrets and configuration

- **Fail fast:** the app refuses to start if a required secret is missing or malformed, and the
  message names the setting and how to generate it (`scripts/init_env.py`).
- **The Jira integration and the digest are optional at startup:** without their credentials the
  app still runs, and the UI explains what isn't configured.
- **Encryption key rotation:** prepend a new key to `ENCRYPTION_KEYS`, run
  `uv run python -m app.db.rotate_keys` to re-encrypt every row with it, then remove the old key.
- **Referential integrity:** SQLite foreign keys are enabled on every connection, so deleting a
  user cascades to their Jira connection, API keys, findings and digest subscriptions.

### Data retention

A daily job (`app/services/maintenance.py`, shortly after startup and then every 24 hours) deletes
data IdentityHub no longer needs:

| Data | Kept for | Why |
|---|---|---|
| `finding` (audit record of created tickets) | `FINDING_RETENTION_DAYS`, default 365 | A year of audit history is a common baseline for security logs. The tickets themselves live in Jira, under the customer's own retention. |
| Login sessions | Until they expire (8 hours) | Expired sessions were already rejected; now their rows are removed too. |
| Revoked and expired API keys | Kept | Their hashes are useless to an attacker, and keeping them lets the UI show the key that is gone. A production version would purge them after a grace period. |
| Digest posts and deliveries | Kept | They are the "already filed" record that prevents duplicate tickets. They grow by one blog post per few days. |

Production would make retention a per-customer setting (some must keep audit data longer, others
shorter), log each purge, and support deletion on request (e.g. when a customer offboards).

---

## 11. Jira rate limits

### How Jira limits apps

Since March 2026, Jira Cloud meters OAuth apps in **points per hour**
([Atlassian: rate limiting](https://developer.atlassian.com/cloud/jira/platform/rate-limiting/)).
Every request costs points: a base cost, plus more per object it returns (each project, issue or
issue type). On top of the hourly quota there are per-second burst limits per site and endpoint,
and a limit on writes to a single issue.

| Tier | Quota | Who |
|---|---|---|
| Tier 1 (default) | **65,000 points/hour, shared by every customer of the app** | All apps |
| Tier 2 | A separate quota per customer site, scaling with its size (e.g. Enterprise: 150,000 + 30 per user, up to 500,000/hour) | Apps Atlassian approves after a review |

The digest files with subscribers' OAuth connections, so it shares the same quota. (An Atlassian
service account or API-token user, as production would use, is outside the OAuth points quota;
only the burst limits apply.)

### What IdentityHub costs, and where it breaks

Approximate points per action before this work: **~51** to open the dashboard (project search, 50
projects), **~11** for the recent-tickets list, **~7** to create a ticket (issue-type lookup and
the create). On Tier 1 that means trouble at roughly **175 active users an hour**, or **~9,000
tickets an hour** across all customers. A few busy scanners on the API could use up the quota for
everyone. The limit is shared, so one customer's burst becomes every customer's outage.

### What's implemented

- **Fewer, cheaper calls:**
  - the issue type for each project is cached for an hour and dropped if Jira rejects a create
    with it (a project's configuration changed);
  - each user's project lists are cached for 5 minutes;
  - the picker loads 20 projects rather than 50.

  In a steady state, creating a ticket is one Jira call.
- **Each 429 handled by its cause** (`RateLimit-Reason`), in `app/jira/client.py`:
  - **Burst limits** are retried up to 3 times, waiting `Retry-After` plus random jitter (tenacity),
    if the wait is 5 seconds or less. Otherwise the user gets a 429 with `Retry-After`.
  - **An exhausted hourly quota** opens a circuit breaker until `Retry-After`. Calls to that pool
    are refused immediately with a clear message: every customer for the global pool, one site for a
    per-site pool. Retrying wouldn't succeed before the reset and would only waste threads.
  - atlassian-python-api's own 429 handling is switched off. By default it sleeps for
    `Retry-After` (up to minutes) inside a worker thread, and it stacks its retries on top of ours.
- **Early warning:** responses whose `RateLimit` headers say the limit is near are logged (at most
  once a minute).
- **The digest is spread out:** each run starts at a random time within `DIGEST_JITTER_MINUTES`
  (default 30) after `DIGEST_DAILY_AT`. This follows Atlassian's advice not to schedule work on
  the hour.

### In a production setting

- **Apply for Tier 2.** It is the only change that gives each customer their own quota, so one
  customer can no longer use up another's.
- **A points budget per customer, enforced at our API.** Before Tier 2, divide the shared quota
  between customers, and give scanner traffic its own allowance so it can't starve interactive
  users. Refuse requests at our edge (429 + `Retry-After`) before they reach Jira.
- **Queue API submissions.** Accept a finding with `202 Accepted` and a status URL, and create the
  ticket from a worker that paces itself against the budget. A scanner sending a burst of 5,000
  findings drains slowly instead of failing.
- **Webhooks instead of polling.** Keep a local copy of each project's IdentityHub tickets,
  updated by Jira webhooks (created, updated, deleted). Recent tickets and the deleted flag then
  cost no Jira calls.
- **Shared state.** Caches and the circuit breaker are in memory, per process. With several
  replicas they belong in Redis, so one replica's 429 protects the others.
- **Metrics and alerts** from the `RateLimit` headers: points used per customer and per endpoint,
  with an alert well before the quota is reached.
- **Never load-test against real customer sites.** Atlassian counts it against the quota and
  warns that it may block the app.

---

## 12. Known limitations and production next steps

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
- **No idempotency on create:** a retried request creates a second ticket. The UI disables the button
  while submitting; the API could accept an `Idempotency-Key` header.
- **API-key rate limits are in memory** (single process), like the login limit.
- **Jira quota protection is per process** and IdentityHub runs on the shared Tier 1 pool
  (section 11).
- **Digest POC conveniences:** filing with a subscriber's account, a local CPU model, extractive
  summaries (section 9, *In a production setting*).
- **The digest scheduler runs in-process,** with its last-run status in memory (summaries and
  deliveries are in the database). With several replicas, run it in one (a leader lock, or a
  separate cron job calling the same function).
- **Stored summaries are never regenerated,** even if a better summarizer is configured later.
  A re-summarize command would update `digest_post.summary` in place. (Deleting the row is not the
  way: it cascades to the post's deliveries, so the post would be filed again.)
- **Jira refresh lock is per process;** multiple workers need a distributed lock (section 4).
- **The full schema at `/docs` is public** and its "Try it out" can't call the cookie-authenticated
  endpoints (no CSRF header). Integrators use `/api/v1/docs`; production would disable `/docs`
  outside development.
