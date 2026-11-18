"""
NEXUS AI · app/integrations
Generic connector framework for external ticketing systems.

Every source system (ServiceNow, Jira, Freshservice, a custom REST API…) is a
"connector": a small class that translates between that system and NEXUS's
common ExternalTicket format. The sync engine, API, agents and UI never talk
to a source system directly, so adding a source means adding one connector.

    schemas.py      ExternalTicket — the common format every connector returns
    base.py         TicketConnector — the class every connector extends
    registry.py     @register_connector and the lookup used by the sync engine
    mapping.py      config-driven value mapping (priority "1" → "critical", …)
    secrets.py      credentials are read from INTEGRATION_* environment variables
    sync.py         fetch → upsert → run agents → write results back
    connectors/     one module per source system
"""

# Importing the connectors package registers every built-in connector.
from app.integrations import connectors  # noqa: F401
