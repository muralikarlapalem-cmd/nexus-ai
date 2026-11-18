"""
NEXUS AI · app/integrations/schemas.py
The common ticket format. Every connector converts its source records into
ExternalTicket, so the rest of NEXUS only ever handles one shape.
"""

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# NEXUS's own vocabularies (same values as the Ticket model enums).
CanonicalPriority = Literal["low", "medium", "high", "critical"]
CanonicalCategory = Literal[
    "network", "hardware", "software", "security", "access", "performance", "database", "other"
]
CanonicalStatus = Literal[
    "open", "triaged", "in_progress", "awaiting_user", "resolved", "closed", "cancelled"
]


class ExternalTicket(BaseModel):
    """A ticket as read from a source system, already mapped to NEXUS values."""

    external_id: str = Field(..., description="Stable ID in the source system (ServiceNow sys_id, Jira key…)")
    external_number: str | None = Field(None, description="Human-readable number, e.g. INC0010023")
    external_url: str | None = Field(None, description="Link that opens the ticket in the source system")

    title: str
    description: str = ""
    priority: CanonicalPriority = "medium"
    category: CanonicalCategory = "other"
    status: CanonicalStatus = "open"

    reporter: str | None = None
    assignment_group: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    # The untouched source record, for debugging and for connectors that need it later.
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)

    def content_hash(self) -> str:
        """
        Fingerprint of the fields NEXUS stores. Re-syncing an unchanged ticket
        (for example after NEXUS posts a work note, which bumps the source's
        "updated" time) gives the same hash and is skipped.
        """
        payload = json.dumps(
            {
                "title": self.title,
                "description": self.description,
                "priority": self.priority,
                "category": self.category,
                "status": self.status,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class ConnectionTestResult(BaseModel):
    ok: bool
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ConfigField(BaseModel):
    """Describes one setting a connector needs, so the UI can draw the form."""

    key: str
    label: str
    type: Literal["string", "url", "number", "boolean", "select", "env_var", "json"] = "string"
    required: bool = False
    default: Any = None
    help: str | None = None
    options: list[str] | None = None   # for type="select"
