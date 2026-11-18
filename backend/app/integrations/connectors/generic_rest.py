"""
NEXUS AI · app/integrations/connectors/generic_rest.py
A connector configured entirely from settings, for any JSON REST API that can
list tickets changed since a time. No code is needed for a new source if its
API fits this shape; write a dedicated connector only when it doesn't.

Example config (Jira Service Management-like API):

    {
      "base_url": "https://acme.atlassian.net",
      "list_path": "/rest/api/3/search",
      "auth_type": "basic", "username": "bot@acme.com", "password_env": "INTEGRATION_JIRA_TOKEN",
      "since_param": "updatedSince", "since_format": "iso",
      "extra_params": {"jql": "project = ITSM"},
      "items_path": "issues",
      "field_map": {
        "external_id": "id", "external_number": "key",
        "title": "fields.summary", "description": "fields.description",
        "priority": "fields.priority.name", "status": "fields.status.name",
        "updated_at": "fields.updated"
      },
      "ticket_url_template": "https://acme.atlassian.net/browse/{external_number}",
      "note_method": "POST",
      "note_path_template": "/rest/api/3/issue/{external_id}/comment",
      "note_body_template": {"body": "{text}"}
    }

Paths use dots ("fields.priority.name"); list items can be indexed ("labels.0").
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.integrations.base import ConnectorError, TicketConnector
from app.integrations.registry import register_connector
from app.integrations.schemas import ConfigField, ConnectionTestResult, ExternalTicket
from app.integrations.secrets import SecretError, read_secret

REQUIRED_FIELDS = ("external_id", "title")


def get_path(data: Any, path: str | None) -> Any:
    """Read 'a.b.0.c' from nested dicts/lists. Returns None if any step is missing."""
    if path is None or path == "":
        return data
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
        if current is None:
            return None
    return current


def _fill_template(value: Any, replacements: dict[str, str]) -> Any:
    """Replace {placeholders} inside every string of a JSON-like structure."""
    if isinstance(value, str):
        for key, text in replacements.items():
            value = value.replace("{" + key + "}", text)
        return value
    if isinstance(value, dict):
        return {k: _fill_template(v, replacements) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill_template(v, replacements) for v in value]
    return value


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value   # epoch ms or s
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    text = str(value)
    for fmt in ("%Y-%m-%d %H:%M:%S",):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@register_connector
class GenericRestConnector(TicketConnector):
    type = "generic_rest"
    display_name = "Generic REST API"
    description = ("Any JSON REST API, set up with field paths instead of code. "
                   "Use it for systems without a dedicated connector.")
    supports_webhook = True

    config_fields = [
        ConfigField(key="base_url", label="Base URL", type="url", required=True),
        ConfigField(key="list_path", label="List endpoint path", required=True,
                    help="Path that returns tickets, e.g. /api/v2/tickets"),
        ConfigField(key="auth_type", label="Authentication", type="select", default="none",
                    options=["none", "basic", "bearer", "header"]),
        ConfigField(key="username", label="Username"),
        ConfigField(key="password_env", label="Password variable", type="env_var"),
        ConfigField(key="token_env", label="Token variable", type="env_var"),
        ConfigField(key="header_name", label="API key header name", help="For auth = header, e.g. X-API-Key"),
        ConfigField(key="since_param", label="'Changed since' query parameter",
                    help="Leave empty if the API can't filter; NEXUS then skips unchanged tickets itself"),
        ConfigField(key="since_format", label="'Changed since' format", type="select", default="iso",
                    options=["iso", "epoch", "epoch_ms", "servicenow"]),
        ConfigField(key="extra_params", label="Extra query parameters", type="json", default={}),
        ConfigField(key="items_path", label="Path to the ticket list in the response",
                    help="e.g. data.items — leave empty if the response is the list itself"),
        ConfigField(key="field_map", label="Field paths", type="json", required=True,
                    help="Where each NEXUS field is in a ticket record: external_id, external_number, title, "
                         "description, priority, category, status, reporter, created_at, updated_at"),
        ConfigField(key="ticket_url_template", label="Ticket link template",
                    help="e.g. https://acme.example.com/tickets/{external_number}"),
        ConfigField(key="offset_param", label="Paging: offset parameter"),
        ConfigField(key="limit_param", label="Paging: page-size parameter"),
        ConfigField(key="page_size", label="Page size", type="number", default=100),
        ConfigField(key="initial_sync_days", label="First sync: days of history", type="number", default=30),
        ConfigField(key="note_method", label="Write-back HTTP method", type="select", default="POST",
                    options=["POST", "PATCH", "PUT"]),
        ConfigField(key="note_path_template", label="Write-back path",
                    help="e.g. /api/v2/tickets/{external_id}/notes — empty disables write-back"),
        ConfigField(key="note_body_template", label="Write-back body", type="json",
                    default={"body": "{text}"}, help="JSON with {text} where the note goes"),
    ]

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> dict[str, Any]:
        cleaned = super().validate_config(config)
        field_map = cleaned.get("field_map")
        if not isinstance(field_map, dict):
            raise ValueError("'Field paths' must be a JSON object")
        missing = [f for f in REQUIRED_FIELDS if not field_map.get(f)]
        if missing:
            raise ValueError(f"'Field paths' must include {missing}")
        return cleaned

    # ── HTTP ──────────────────────────────────────────────────
    def _client(self) -> httpx.AsyncClient:
        headers = {"Accept": "application/json"}
        kwargs: dict[str, Any] = {}
        auth_type = self.config.get("auth_type") or "none"
        try:
            if auth_type == "basic":
                kwargs["auth"] = (self.config.get("username") or "", read_secret(self.config.get("password_env") or ""))
            elif auth_type == "bearer":
                headers["Authorization"] = f"Bearer {read_secret(self.config.get('token_env') or '')}"
            elif auth_type == "header":
                headers[self.config.get("header_name") or "X-API-Key"] = read_secret(self.config.get("token_env") or "")
        except SecretError as e:
            raise ConnectorError(str(e)) from e
        return self.http_client(base_url=str(self.config["base_url"]).rstrip("/"), headers=headers, **kwargs)

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            async with self._client() as client:
                response = await client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise ConnectorError(f"Could not reach {self.config['base_url']}: {e}") from e
        if response.status_code in (401, 403):
            raise ConnectorError(f"The API rejected the credentials ({response.status_code}).")
        if response.status_code >= 400:
            raise ConnectorError(f"The API returned {response.status_code}: {response.text[:300]}")
        return response

    # ── Mapping ───────────────────────────────────────────────
    def to_external_ticket(self, record: dict[str, Any]) -> ExternalTicket:
        fm: dict[str, str] = self.config["field_map"]

        def field(name: str) -> Any:
            return get_path(record, fm.get(name)) if fm.get(name) else None

        external_id = field("external_id")
        if external_id in (None, ""):
            raise ConnectorError(f"Record has no value at '{fm.get('external_id')}' (external_id)")
        title = str(field("title") or "").strip() or f"Ticket {external_id}"
        number = field("external_number")
        description = field("description")
        url = None
        if self.config.get("ticket_url_template"):
            url = self.config["ticket_url_template"].format(
                external_id=external_id, external_number=number or external_id)
        return ExternalTicket(
            external_id=str(external_id),
            external_number=str(number) if number not in (None, "") else None,
            external_url=url,
            title=title[:500],
            description=str(description).strip() if description else title,
            priority=self.mapper.to_nexus("priority", field("priority")),
            category=self.mapper.to_nexus("category", field("category")),
            status=self.mapper.to_nexus("status", field("status")),
            reporter=str(field("reporter")) if field("reporter") else None,
            created_at=_parse_dt(field("created_at")),
            updated_at=_parse_dt(field("updated_at")),
            raw=record,
        )

    def _format_since(self, since: datetime) -> str:
        fmt = self.config.get("since_format") or "iso"
        since = since.astimezone(timezone.utc)
        if fmt == "epoch":
            return str(int(since.timestamp()))
        if fmt == "epoch_ms":
            return str(int(since.timestamp() * 1000))
        if fmt == "servicenow":
            return since.strftime("%Y-%m-%d %H:%M:%S")
        return since.isoformat()

    # ── The contract ──────────────────────────────────────────
    async def test_connection(self) -> ConnectionTestResult:
        try:
            params = dict(self.config.get("extra_params") or {})
            if self.config.get("limit_param"):
                params[self.config["limit_param"]] = 1
            response = await self._request("GET", self.config["list_path"], params=params)
            items = get_path(response.json(), self.config.get("items_path"))
            if not isinstance(items, list):
                return ConnectionTestResult(
                    ok=False, message=f"Connected, but found no list at '{self.config.get('items_path') or '(root)'}'")
            if items:
                self.to_external_ticket(items[0])   # proves the field paths work
        except ConnectorError as e:
            return ConnectionTestResult(ok=False, message=str(e))
        return ConnectionTestResult(ok=True, message="Connected, and the field paths read a ticket correctly.")

    async def fetch_updated(self, since: datetime | None, limit: int = 500) -> list[ExternalTicket]:
        if since is None:
            days = int(self.config.get("initial_sync_days") or 0)
            if days > 0:
                since = datetime.now(timezone.utc) - timedelta(days=days)
        params = dict(self.config.get("extra_params") or {})
        if since is not None and self.config.get("since_param"):
            params[self.config["since_param"]] = self._format_since(since)

        page_size = int(self.config.get("page_size") or 100)
        paged = bool(self.config.get("offset_param") and self.config.get("limit_param"))
        tickets: list[ExternalTicket] = []
        offset = 0
        while len(tickets) < limit:
            if paged:
                params[self.config["offset_param"]] = offset
                params[self.config["limit_param"]] = page_size
            response = await self._request("GET", self.config["list_path"], params=params)
            items = get_path(response.json(), self.config.get("items_path")) or []
            if not isinstance(items, list):
                raise ConnectorError(f"No list found at '{self.config.get('items_path') or '(root)'}'")
            for item in items:
                ticket = self.to_external_ticket(item)
                # APIs without a "since" filter: drop tickets that haven't changed
                if since and ticket.updated_at and ticket.updated_at <= since:
                    continue
                tickets.append(ticket)
            if not paged or len(items) < page_size:
                break
            offset += len(items)
        tickets.sort(key=lambda t: t.updated_at or datetime.min.replace(tzinfo=timezone.utc))
        return tickets[:limit]

    async def post_note(self, ticket: ExternalTicket | str, text: str) -> None:
        template = self.config.get("note_path_template")
        if not template:
            raise ConnectorError("Write-back is not set up for this connection (no write-back path)")
        if isinstance(ticket, ExternalTicket):
            ids = {"external_id": ticket.external_id, "external_number": ticket.external_number or ticket.external_id}
        else:
            ids = {"external_id": ticket, "external_number": ticket}
        path = template.format(**ids)
        body = _fill_template(self.config.get("note_body_template") or {"body": "{text}"}, {"text": text})
        await self._request(self.config.get("note_method") or "POST", path, json=body)

    def parse_webhook(self, payload: dict[str, Any]) -> list[ExternalTicket]:
        items = get_path(payload, self.config.get("items_path"))
        if isinstance(items, list):
            return [self.to_external_ticket(i) for i in items]
        return [self.to_external_ticket(payload)]
