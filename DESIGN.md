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
| **SQLite** | Zero setup for reviewers. All access goes through SQLAlchemy, so moving to Postgres is a connection-string change (plus migrations, see section 11). |
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
  an org-level Jira identity is appropriate, and the blog digest's bot account (section 9) is
  exactly this pattern.

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
- **Searchable, 50 at a time.** The picker filters by name or key on the server; users with many
  projects type to narrow the list rather than scrolling through hundreds.

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
- **Jira is the source of truth:** renamed, moved or deleted issues show correctly, and tickets
  created through the REST API appear too.
- **It runs as the user,** so it shows only issues they are allowed to see. That includes tickets
  that *other* IdentityHub users created in the same project, which is what "created from this
  app" means. Showing only the user's own tickets would be a one-line change
  (`AND reporter = currentUser()`).
- **Caveat:** anyone can add the `identityhub` label to an issue by hand. Atlassian's
  non-editable alternative, indexed issue properties, is only available to Forge and Connect apps.
- **JQL injection:** the project key is validated against `^[A-Z][A-Z0-9_]{1,19}$` before it is
  placed in the query.
- A local `finding` table also records every ticket created (who, from the UI or API, which key),
  as an audit trail independent of Jira.

### Jira errors users will actually see

| Jira says | User sees | HTTP |
|---|---|---|
| 400 (e.g. required field) | "Jira rejected the request: *Jira's reason*" | 422 |
| 401 | "Reconnect Jira" (connection marked `needs_reauth`) | 409 |
| 403 | "Your Jira account doesn't have permission to create issues in SEC." | 403 |
| 404 | "Project SEC wasn't found, or your Jira account can't access it." | 404 |
| 429 | "Jira is receiving too many requests right now…" | 429 |
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
| Report finding | Searchable project picker, the finding form, and the 10 recent tickets for the chosen project side by side (stacked on phones). |
| Settings | **Jira connection:** connected site and account, reconnect or switch account, disconnect (with confirmation), choose a site, and the outcome of the OAuth redirect. **NHI Blog Digest:** recipient projects, last ticket or error per project, last run, *Run now*. **API keys:** create (permissions locked once created), one-time reveal, notes, revoke. |

### Interaction decisions

- **The UI follows the Jira connection state.** Each state gets one clear next step instead of a
  form that would fail: not configured on the server, *Connect Jira*, *Choose a site*,
  *Reconnect Jira*, or the form. If Jira rejects the token mid-request, the banner switches to
  *Reconnect Jira* without a reload.
- **Project picker:** searches Jira as you type (debounced), lists only projects you can create
  issues in, and remembers your last project per user in this browser.
- **The form** is disabled until a project is chosen, validates instantly with the same limits as
  the server (the server stays the authority), and disables the submit button while sending so a
  double click can't create two tickets. On success it shows the new key with an *Open in Jira*
  link, clears the fields, keeps the project, and refreshes the recent list. On failure it keeps
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
  60 per minute per API key and 120 per IP on `/api/v1`; 3 per minute on the digest's *Run now*.
  All answered with `429` and `Retry-After`.
- **Security headers:** strict Content-Security-Policy (`script-src 'self'`, no framing),
  `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy` and `Permissions-Policy`; HSTS only when
  served over https. The `/docs` page gets a relaxed policy so Swagger UI can load from its CDN.

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
- **Documented in OpenAPI** (`/docs`), including the bearer scheme and every error status.
  Swagger's *Authorize* button works here, because this endpoint doesn't need CSRF.

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
| The **bot account** that files tickets, and the **summarizer** (Claude key / local model) | The deployer, once | `.env`, like the Atlassian OAuth secrets |
| **Which projects** receive the digest | Each user, for projects they work in | Settings → NHI Blog Digest |

The automation itself has no UI, as the brief specifies: it is a scheduled job inside the server.
The Settings section only lets users choose recipient projects and see what happened (last ticket
per project, errors, last run, and a *Run now* button for demos).

### The bot account

- **Tickets come from IdentityHub, not from a person.** A 3LO OAuth app always acts as the user who
  authorized it; there is no "app user" to attribute a ticket to (only Forge or Connect apps have
  one, a different distribution model, section 3). So a dedicated Atlassian account named e.g.
  "IdentityHub" files the tickets, authenticated with an API token set at deploy time.
- **The bot can't widen anyone's access.**
  - The picker only offers projects that **both** the user and the bot can create issues in.
  - Subscribing is re-validated on the server.
  - At delivery, at least one subscriber must **still** be able to create issues in the project;
    otherwise nothing is filed and the subscription shows why.
- **Permissions are granted in Jira and verified by us.** A Jira admin gives the bot *Create Issues*
  on the digest projects (ideally nothing else; a classic API token carries all of its account's
  permissions). IdentityHub can't grant Jira permissions; it checks them:
  - The bot's sign-in is verified (and re-checked every few minutes).
  - Its per-project access is checked when subscribing and on every run.
  - Each failure has its own message: wrong site URL, rejected credentials, Jira unreachable, or
    "the bot can no longer create issues in OPS. Ask a Jira admin to grant it access."
- **One site.** The bot works on one Jira site; users connected to a different site are told so.
- **Production:** Atlassian's dedicated service accounts and scoped API tokens, where available on
  the plan, would narrow the bot further. The bot uses a seat; Jira's free plan allows 10 users.

### The run

- **When:** daily at a fixed time, `DIGEST_DAILY_AT` (09:00 UTC by default), so restarts don't
  shift the schedule. A catch-up run also happens shortly after the server starts, in case it was
  down at the scheduled time; it is cheap because filed posts and stored summaries are reused.
  *Run now* in Settings triggers the same function.
- **Which posts:** the blog has no RSS feed, and its index pins an older featured post at the top.
  The digest takes the first 8 post links and reads each post's schema.org `BlogPosting` JSON-LD
  for its `datePublished` (the post pages show no visible date of their own); `trafilatura`
  extracts the article text.
- **What each project is due (catch-up and fresh start).** Every project has a *watermark*:
  - the publish date of the newest post already filed there, or
  - when its current subscriptions began, if that is later. That is the **fresh start**: a new
    subscription receives posts published after subscribing, not the existing backlog.
  - Optionally, **"Also send the latest blog post now"** (a checkbox when adding projects) moves a
    new subscription's start back to just before the newest post that already existed, and starts
    a run immediately: the project gets that one post now, still not the whole backlog. It only
    applies to projects added in that save, never to existing subscriptions.

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
  project's recent tickets list), a link to the post, its publish date, and which summarizer wrote
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

**The blog is untrusted input.** The article is fenced in `<article>` tags, and the prompt says to
treat it as content, never as instructions. The model has no tools and its output only becomes
ticket text, so a prompt injection in a post could at worst produce a misleading summary.

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

---

## 11. Known limitations and production next steps

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
- **No idempotency on create:** a retried request creates a second ticket. The UI disables the button
  while submitting; the API could accept an `Idempotency-Key` header.
- **API-key rate limits are in memory** (single process), like the login limit.
- **The digest scheduler runs in-process,** with its last-run status in memory (summaries and
  deliveries are in the database). With several replicas, run it in one (a leader lock, or a
  separate cron job calling the same function).
- **Stored summaries are never regenerated,** even if a better summarizer is configured later.
  A re-summarize command would update `digest_post.summary` in place. (Deleting the row is not the
  way: it cascades to the post's deliveries, so the post would be filed again.)
- **Jira refresh lock is per process;** multiple workers need a distributed lock (section 4).
- **Swagger UI's "Try it out"** can't call cookie-authenticated endpoints, because it doesn't send
  the CSRF header. It is intended for the API-key REST API.
