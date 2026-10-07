"""Request/response contracts for API keys, including the permissions model.

How API key permissions evolve
------------------------------
A key's permissions are one JSON document, fixed at creation and never modified:

    {"version": 1, "scopes": ["findings:create"], "projects": ["SEC", "PLAT"]}
    {"version": 1, "scopes": ["findings:create"], "projects": "all"}

New permission options must not change what existing keys can do:

* A new **action** becomes a new `Scope` value. Existing keys don't hold it, so they can't do it.
* A new **restriction** (e.g. an IP allowlist) becomes an optional field whose absence means "not
  restricted in this dimension", so existing keys behave exactly as before.
* A change to the meaning of an existing field requires bumping `version` and handling both.

`"projects": "all"` was added after the list form. It means every project the key's owner can
create issues in, including ones they gain access to later (Jira still enforces the owner's
permissions on every request). Existing keys are unaffected, and a release that predates it rejects
such a key rather than misreading it (fail-closed, below).

Reading is fail-closed: a stored document with a field this code doesn't know (e.g. written by a
newer release, then rolled back) is rejected rather than having the restriction silently ignored.
"""

import enum
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.findings import ProjectKey

# Every key expires; these are the lifetimes offered. No "never expires" option: a leaked key in a
# CI log or scanner config should stop working on its own.
LIFETIME_DAYS = (7, 30, 90, 180, 365)
MAX_PROJECTS_PER_KEY = 50  # also Jira's limit for filtering projects by key in one request
PERMISSIONS_VERSION = 1
ALL_PROJECTS = "all"

ApiKeyStatus = Literal["active", "expired", "revoked"]


class Scope(enum.StrEnum):
    """Actions an API key can be granted. Add new actions here; never repurpose a value."""

    FINDINGS_CREATE = "findings:create"


class ApiKeyPermissions(BaseModel):
    """What a key may do. Validated the same way when created and every time it is used."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = PERMISSIONS_VERSION
    scopes: Annotated[list[Scope], Field(min_length=1, description="Actions the key may perform.")]
    projects: Annotated[
        Annotated[list[ProjectKey], Field(min_length=1, max_length=MAX_PROJECTS_PER_KEY)] | Literal["all"],
        Field(
            description=(
                'Jira projects the key may create tickets in, or "all": every project the key\'s owner can '
                "create issues in, now or later."
            ),
            examples=[["SEC"], "all"],
        ),
    ]

    @property
    def all_projects(self) -> bool:
        return self.projects == ALL_PROJECTS

    @field_validator("scopes", mode="before")
    @classmethod
    def _dedupe_scopes(cls, value: object) -> object:
        return list(dict.fromkeys(value)) if isinstance(value, list) else value

    @field_validator("projects", mode="before")
    @classmethod
    def _normalise_projects(cls, value: object) -> object:
        # Upper-case and drop duplicates, keeping the order the user chose.
        if not isinstance(value, list):
            return value
        return list(dict.fromkeys(v.strip().upper() if isinstance(v, str) else v for v in value))


Notes = Annotated[str | None, Field(max_length=1000, description="What the key is used for. Editable later.")]


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=100, examples=["GitHub Actions - infra repo"])]
    notes: Notes = None
    expires_in_days: Literal[7, 30, 90, 180, 365] = 90
    permissions: ApiKeyPermissions


class ApiKeyUpdate(BaseModel):
    """Only notes can change after creation. Permissions, expiry and name are fixed: sending them
    is rejected (extra="forbid") rather than silently ignored."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    notes: Notes


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    name: str
    notes: str | None
    prefix: str = Field(description="First characters of the key, to tell keys apart. Not secret.")
    permissions: ApiKeyPermissions
    status: ApiKeyStatus
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="The full API key. Shown only once; store it securely.")
