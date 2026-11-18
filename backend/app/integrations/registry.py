"""
NEXUS AI · app/integrations/registry.py
Keeps track of the available connector types.

    @register_connector
    class JiraConnector(TicketConnector):
        type = "jira"
        ...

The sync engine then builds the right connector for a connection row with
build_connector(connection), without knowing which source it is.
"""

from typing import Any

import httpx

from app.integrations.base import TicketConnector

_REGISTRY: dict[str, type[TicketConnector]] = {}

# Tests set this so every connector talks to an in-process mock server.
HTTP_TRANSPORT_OVERRIDE: httpx.AsyncBaseTransport | None = None


def register_connector(cls: type[TicketConnector]) -> type[TicketConnector]:
    if not getattr(cls, "type", None):
        raise TypeError(f"{cls.__name__} must set a 'type' class attribute")
    if cls.type in _REGISTRY and _REGISTRY[cls.type] is not cls:
        raise ValueError(f"Connector type '{cls.type}' is already registered")
    _REGISTRY[cls.type] = cls
    return cls


def get_connector_class(connector_type: str) -> type[TicketConnector]:
    try:
        return _REGISTRY[connector_type]
    except KeyError:
        raise ValueError(
            f"Unknown connector type '{connector_type}'. Available: {sorted(_REGISTRY)}"
        ) from None


def list_connector_types() -> list[dict[str, Any]]:
    return [cls.describe() for cls in sorted(_REGISTRY.values(), key=lambda c: c.display_name)]


def build_connector(connector_type: str, config: dict[str, Any]) -> TicketConnector:
    cls = get_connector_class(connector_type)
    return cls(config, transport=HTTP_TRANSPORT_OVERRIDE)
