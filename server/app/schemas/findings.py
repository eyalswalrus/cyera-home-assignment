"""Request/response contracts for NHI findings, shared by the UI routes and the public REST API."""

import enum
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Jira project keys: an uppercase letter followed by uppercase letters, digits or underscores.
PROJECT_KEY_PATTERN = r"^[A-Z][A-Z0-9_]{1,19}$"


class FindingType(enum.StrEnum):
    STALE_IDENTITY = "stale_identity"
    OVERPRIVILEGED = "overprivileged"
    EXPIRING_CREDENTIAL = "expiring_credential"
    EXPOSED_SECRET = "exposed_secret"
    OTHER = "other"

    @property
    def label(self) -> str:
        return {
            "stale_identity": "Stale identity",
            "overprivileged": "Over-privileged",
            "expiring_credential": "Expiring credential",
            "exposed_secret": "Exposed secret",
            "other": "Other",
        }[self.value]


class Severity(enum.StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


ProjectKey = Annotated[str, Field(pattern=PROJECT_KEY_PATTERN, examples=["SEC"])]


class FindingCreate(BaseModel):
    # Unknown fields are rejected so a typo (e.g. "sumary") fails loudly instead of being ignored.
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    project_key: ProjectKey
    summary: Annotated[
        str, Field(min_length=1, max_length=255, examples=["Stale Service Account: svc-deploy-prod"])
    ]
    description: Annotated[str, Field(max_length=30_000)] = ""
    finding_type: FindingType | None = None
    severity: Severity | None = None
    identity_name: Annotated[str | None, Field(max_length=255, examples=["svc-deploy-prod"])] = None

    @field_validator("project_key", mode="before")
    @classmethod
    def _uppercase_key(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("summary")
    @classmethod
    def _single_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("must be a single line")
        return value


class FindingCreated(BaseModel):
    key: str = Field(examples=["SEC-42"])
    url: str = Field(examples=["https://acme.atlassian.net/browse/SEC-42"])
    summary: str


class RecentTicket(BaseModel):
    key: str
    summary: str
    url: str | None = Field(description="Link to the issue; null when it's no longer in Jira")
    created_at: datetime
    deleted: bool = Field(
        default=False,
        description="You created this ticket, but Jira no longer returns it (deleted, moved, or you lost access)",
    )


class Project(BaseModel):
    id: str
    key: str
    name: str
