"""
NEXUS AI · app/integrations/connectors/servicenow.py
ServiceNow connector, using the standard Table API:

    GET   /api/now/table/incident?sysparm_query=sys_updated_on>…^ORDERBYsys_updated_on
    PATCH /api/now/table/incident/{sys_id}   {"work_notes": "..."}

Works the same against the NEXUS mock (docker service "servicenow-mock") and a
real instance, including a free Personal Developer Instance from
developer.servicenow.com. Only the URL and credentials change.

Time zone: ServiceNow returns sys_updated_on in UTC, but reads dates in an
encoded query in the API user's time zone. Set the integration user's time
zone to GMT in ServiceNow so the "changed since" filter lines up. The sync
also re-reads a few minutes of overlap, and unchanged tickets are skipped, so
small clock differences don't lose or duplicate anything.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.integrations.base import ConnectorError, TicketConnector
from app.integrations.registry import register_connector
from app.integrations.schemas import ConfigField, ConnectionTestResult, ExternalTicket
from app.integrations.secrets import SecretError, read_secret

SN_DATETIME = "%Y-%m-%d %H:%M:%S"

FIELDS = [
    "sys_id", "number", "short_description", "description", "priority", "urgency", "impact",
    "category", "subcategory", "state", "caller_id", "assignment_group",
    "sys_created_on", "sys_updated_on",
]


def _value(record: dict[str, Any], field: str) -> str:
    """
    A field's internal value. With sysparm_display_value=all every field is
    {"value": ..., "display_value": ...}; webhooks usually send plain strings.
    """
    raw = record.get(field)
    if isinstance(raw, dict):
        return str(raw.get("value") or "")
    return "" if raw is None else str(raw)


def _display(record: dict[str, Any], field: str) -> str:
    raw = record.get(field)
    if isinstance(raw, dict):
        return str(raw.get("display_value") or raw.get("value") or "")
    return "" if raw is None else str(raw)


def _parse_dt(text: str) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.strptime(text, SN_DATETIME).replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None


@register_connector
class ServiceNowConnector(TicketConnector):
    type = "servicenow"
    display_name = "ServiceNow"
    description = "Incidents from ServiceNow's Table API. Posts NEXUS results back as work notes."
    supports_status_update = True
    supports_webhook = True

    config_fields = [
        ConfigField(key="instance_url", label="Instance URL", type="url", required=True,
                    help="e.g. https://dev12345.service-now.com, or http://servicenow-mock:8090 for the mock"),
        ConfigField(key="browser_url", label="Link URL for people", type="url",
                    help="Base URL used for 'open in ServiceNow' links, if different from the instance URL "
                         "(the mock is servicenow-mock:8090 inside Docker but localhost:8090 in your browser)"),
        ConfigField(key="table", label="Table", default="incident"),
        ConfigField(key="auth_type", label="Authentication", type="select", default="basic",
                    options=["basic", "bearer"]),
        ConfigField(key="username", label="Username", help="Integration user (basic auth)"),
        ConfigField(key="password_env", label="Password variable", type="env_var",
                    default="INTEGRATION_SERVICENOW_PASSWORD",
                    help="Name of the .env variable holding the password"),
        ConfigField(key="token_env", label="OAuth token variable", type="env_var",
                    help="Name of the .env variable holding a bearer token (auth = bearer)"),
        ConfigField(key="query_filter", label="Extra filter",
                    help="Optional encoded query, e.g. assignment_group.name=Service Desk"),
        ConfigField(key="initial_sync_days", label="First sync: days of history", type="number", default=30,
                    help="Only import tickets updated in the last N days on the first sync (0 = everything)"),
        ConfigField(key="page_size", label="Page size", type="number", default=100),
        ConfigField(key="resolve_close_code", label="Close code when resolving", default="Solved (Permanently)",
                    help="Used only if status write-back is turned on"),
    ]

    default_mappings = {
        # ServiceNow priority is derived from impact × urgency: 1 Critical … 5 Planning
        "priority": {"1": "critical", "2": "high", "3": "medium", "4": "low", "5": "low"},
        # Out-of-box incident categories, plus common custom ones
        "category": {
            "inquiry": "other", "software": "software", "hardware": "hardware",
            "network": "network", "database": "database", "security": "security",
            "access": "access", "performance": "performance",
        },
        # 1 New, 2 In Progress, 3 On Hold, 6 Resolved, 7 Closed, 8 Canceled
        "status": {"1": "open", "2": "in_progress", "3": "awaiting_user",
                   "6": "resolved", "7": "closed", "8": "cancelled"},
    }

    # ── Connection helpers ────────────────────────────────────
    @property
    def base_url(self) -> str:
        return str(self.config["instance_url"]).rstrip("/")

    @property
    def table(self) -> str:
        return self.config.get("table") or "incident"

    def _auth_kwargs(self) -> dict[str, Any]:
        try:
            if (self.config.get("auth_type") or "basic") == "bearer":
                token = read_secret(self.config.get("token_env") or "")
                return {"headers": {"Authorization": f"Bearer {token}"}}
            password = read_secret(self.config.get("password_env") or "")
            return {"auth": (self.config.get("username") or "", password)}
        except SecretError as e:
            raise ConnectorError(str(e)) from e

    def _client(self) -> httpx.AsyncClient:
        auth = self._auth_kwargs()
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   **auth.pop("headers", {})}
        return self.http_client(base_url=self.base_url, headers=headers, **auth)

    def ticket_url(self, sys_id: str) -> str:
        base = str(self.config.get("browser_url") or self.base_url).rstrip("/")
        return f"{base}/nav_to.do?uri={self.table}.do?sys_id={sys_id}"

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            async with self._client() as client:
                response = await client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise ConnectorError(f"Could not reach ServiceNow at {self.base_url}: {e}") from e
        if response.status_code == 401:
            raise ConnectorError("ServiceNow rejected the credentials (401). Check the username and password variable.")
        if response.status_code == 403:
            raise ConnectorError(f"The integration user isn't allowed to access the '{self.table}' table (403).")
        if response.status_code >= 400:
            detail = response.text[:300]
            raise ConnectorError(f"ServiceNow returned {response.status_code}: {detail}")
        return response

    # ── Mapping ───────────────────────────────────────────────
    def to_external_ticket(self, record: dict[str, Any]) -> ExternalTicket:
        sys_id = _value(record, "sys_id")
        if not sys_id:
            raise ConnectorError("ServiceNow record has no sys_id")
        title = _value(record, "short_description").strip() or f"{self.table} {_value(record, 'number')}"
        description = _value(record, "description").strip()
        return ExternalTicket(
            external_id=sys_id,
            external_number=_value(record, "number") or None,
            external_url=self.ticket_url(sys_id),
            title=title[:500],
            description=description or title,
            priority=self.mapper.to_nexus("priority", _value(record, "priority")),
            category=self.mapper.to_nexus("category", _value(record, "category")),
            status=self.mapper.to_nexus("status", _value(record, "state")),
            reporter=_display(record, "caller_id") or None,
            assignment_group=_display(record, "assignment_group") or None,
            created_at=_parse_dt(_value(record, "sys_created_on")),
            updated_at=_parse_dt(_value(record, "sys_updated_on")),
            raw=record,
        )

    # ── The contract ──────────────────────────────────────────
    async def test_connection(self) -> ConnectionTestResult:
        try:
            response = await self._request(
                "GET", f"/api/now/table/{self.table}",
                params={"sysparm_limit": 1, "sysparm_fields": "sys_id,number"},
            )
        except ConnectorError as e:
            return ConnectionTestResult(ok=False, message=str(e))
        total = response.headers.get("X-Total-Count")
        return ConnectionTestResult(
            ok=True,
            message=f"Connected to ServiceNow. The '{self.table}' table is readable.",
            details={"records_in_table": int(total) if total and total.isdigit() else None},
        )

    def _build_query(self, since: datetime | None) -> str:
        parts: list[str] = []
        if since is None:
            days = int(self.config.get("initial_sync_days") or 0)
            if days > 0:
                since = datetime.now(timezone.utc) - timedelta(days=days)
        if since is not None:
            parts.append(f"sys_updated_on>{since.astimezone(timezone.utc).strftime(SN_DATETIME)}")
        if self.config.get("query_filter"):
            parts.append(str(self.config["query_filter"]).strip("^"))
        parts.append("ORDERBYsys_updated_on")
        return "^".join(parts)

    async def fetch_updated(self, since: datetime | None, limit: int = 500) -> list[ExternalTicket]:
        page_size = max(1, min(int(self.config.get("page_size") or 100), 1000))
        query = self._build_query(since)
        tickets: list[ExternalTicket] = []
        offset = 0
        while len(tickets) < limit:
            response = await self._request(
                "GET", f"/api/now/table/{self.table}",
                params={
                    "sysparm_query": query,
                    "sysparm_fields": ",".join(FIELDS),
                    "sysparm_display_value": "all",
                    "sysparm_exclude_reference_link": "true",
                    "sysparm_limit": min(page_size, limit - len(tickets)),
                    "sysparm_offset": offset,
                },
            )
            records = response.json().get("result", [])
            tickets.extend(self.to_external_ticket(r) for r in records)
            if len(records) < page_size:
                break
            offset += len(records)
        return tickets

    async def post_note(self, ticket: ExternalTicket | str, text: str) -> None:
        sys_id = ticket.external_id if isinstance(ticket, ExternalTicket) else ticket
        await self._request("PATCH", f"/api/now/table/{self.table}/{sys_id}", json={"work_notes": text})

    async def update_status(self, external_id: str, nexus_status: str, note: str = "") -> None:
        state = self.mapper.to_source("status", nexus_status)
        if state is None:
            raise ConnectorError(f"No ServiceNow state is mapped to NEXUS status '{nexus_status}'")
        body: dict[str, Any] = {"state": state}
        if nexus_status in ("resolved", "closed"):
            body["close_code"] = self.config.get("resolve_close_code") or "Solved (Permanently)"
            body["close_notes"] = note or "Resolved by NEXUS AI."
        await self._request("PATCH", f"/api/now/table/{self.table}/{external_id}", json=body)

    def parse_webhook(self, payload: dict[str, Any]) -> list[ExternalTicket]:
        """
        Accepts what a ServiceNow Business Rule or Flow typically sends:
        a record, {"result": record}, or {"records": [record, ...]}.
        """
        if "records" in payload and isinstance(payload["records"], list):
            records = payload["records"]
        elif "result" in payload and isinstance(payload["result"], (dict, list)):
            records = payload["result"] if isinstance(payload["result"], list) else [payload["result"]]
        else:
            records = [payload]
        return [self.to_external_ticket(r) for r in records]
