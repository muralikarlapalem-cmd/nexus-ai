"""
NEXUS AI · app/integrations/base.py
TicketConnector — the contract every source system implements.

To add a source system, subclass TicketConnector, fill in the four methods,
and put @register_connector on the class (see connectors/servicenow.py).

    test_connection()              Can we reach it with these credentials?
    fetch_updated(since)           Tickets created or changed after `since`
    post_note(ticket, text)        Add an internal note (write-back)
    update_status(ticket, status)  Optional: change the ticket's status

Connectors are stateless: they get their config and credentials at
construction, do HTTP, and return ExternalTicket objects. They never touch
the NEXUS database; the sync engine does that.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, ClassVar

import httpx

from app.integrations.mapping import ValueMapper
from app.integrations.schemas import ConfigField, ConnectionTestResult, ExternalTicket


class ConnectorError(Exception):
    """A source system call failed. The message is shown to admins in the UI."""


class TicketConnector(ABC):
    # ── Class-level description, used by the registry and the UI ──
    type: ClassVar[str]                       # e.g. "servicenow"
    display_name: ClassVar[str]               # e.g. "ServiceNow"
    description: ClassVar[str] = ""
    config_fields: ClassVar[list[ConfigField]] = []
    default_mappings: ClassVar[dict[str, dict[str, str]]] = {}
    supports_status_update: ClassVar[bool] = False
    supports_webhook: ClassVar[bool] = False

    def __init__(
        self,
        config: dict[str, Any],
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.config = config
        self.mapper = ValueMapper(self.default_mappings, config.get("mappings"))
        # Tests inject a transport so connectors can talk to an in-process mock.
        self._transport = transport

    # ── Helpers for subclasses ────────────────────────────────
    def http_client(self, **kwargs) -> httpx.AsyncClient:
        timeout = float(self.config.get("timeout_seconds") or 30)
        return httpx.AsyncClient(transport=self._transport, timeout=timeout, **kwargs)

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> dict[str, Any]:
        """Check required fields and env-var names. Returns the config with defaults filled in."""
        from app.integrations.secrets import validate_env_var_name

        cleaned = dict(config)
        for field in cls.config_fields:
            value = cleaned.get(field.key)
            if value in (None, "") and field.default is not None:
                cleaned[field.key] = field.default
                value = field.default
            if field.required and value in (None, ""):
                raise ValueError(f"'{field.label}' is required")
            if field.type == "env_var" and value:
                validate_env_var_name(value)
            if field.type == "select" and value and field.options and value not in field.options:
                raise ValueError(f"'{field.label}' must be one of {field.options}")
        # Fail early on bad mappings
        ValueMapper(cls.default_mappings, cleaned.get("mappings"))
        return cleaned

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {
            "type": cls.type,
            "display_name": cls.display_name,
            "description": cls.description,
            "config_fields": [f.model_dump() for f in cls.config_fields],
            "default_mappings": cls.default_mappings,
            "supports_status_update": cls.supports_status_update,
            "supports_webhook": cls.supports_webhook,
        }

    # ── The contract ──────────────────────────────────────────
    @abstractmethod
    async def test_connection(self) -> ConnectionTestResult: ...

    @abstractmethod
    async def fetch_updated(self, since: datetime | None, limit: int = 500) -> list[ExternalTicket]:
        """Return tickets created or updated after `since` (all tickets if None), oldest first."""

    @abstractmethod
    async def post_note(self, ticket: ExternalTicket | str, text: str) -> None:
        """Add an internal note to the ticket. `ticket` is an ExternalTicket or its external_id."""

    async def update_status(self, external_id: str, nexus_status: str, note: str = "") -> None:
        raise ConnectorError(f"{self.display_name} connector does not support status updates")

    def parse_webhook(self, payload: dict[str, Any]) -> list[ExternalTicket]:
        """Turn a webhook body into tickets. Connectors that support push override this."""
        raise ConnectorError(f"{self.display_name} connector does not accept webhooks")
