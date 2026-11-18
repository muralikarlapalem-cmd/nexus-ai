"""
NEXUS AI · app/integrations/oauth.py
OAuth 2.0 access tokens for connectors, fetched on demand and reused until
shortly before they expire.

Supported grants (both used by ServiceNow, Jira and most ticketing systems):
    client_credentials   client ID + secret only; the token acts as the user the
                         source system ties to the OAuth app
    password             client ID + secret + a user's name and password
                         (works where plain API password logins are switched off)

Tokens live in memory per process (API server, each Celery worker). Nothing is
written to disk or the database. If the source system rejects a cached token
(e.g. it was revoked), the connector calls invalidate() and fetches a new one.
"""

import time
from dataclasses import dataclass

import httpx

# Renew this many seconds before the token's stated expiry
EXPIRY_MARGIN_SECONDS = 60
DEFAULT_LIFETIME_SECONDS = 1800      # ServiceNow's default when a server omits expires_in


class OAuthError(Exception):
    """Token request failed. The message is shown to admins in the UI."""


@dataclass(frozen=True)
class TokenRequest:
    token_url: str
    grant_type: str                  # "client_credentials" | "password"
    client_id: str
    client_secret: str
    username: str | None = None
    password: str | None = None
    scope: str | None = None

    @property
    def cache_key(self) -> tuple:
        # The secret and password are deliberately not part of the key, so they
        # aren't kept around longer than needed. Changing them in .env requires a
        # restart anyway, which clears the cache.
        return (self.token_url, self.grant_type, self.client_id, self.username, self.scope)

    def form(self) -> dict[str, str]:
        data = {"grant_type": self.grant_type, "client_id": self.client_id,
                "client_secret": self.client_secret}
        if self.grant_type == "password":
            data["username"] = self.username or ""
            data["password"] = self.password or ""
        if self.scope:
            data["scope"] = self.scope
        return data


_cache: dict[tuple, tuple[str, float]] = {}


def invalidate(request: TokenRequest) -> None:
    _cache.pop(request.cache_key, None)


def clear_cache() -> None:
    _cache.clear()


async def get_token(request: TokenRequest, client: httpx.AsyncClient) -> str:
    """Return a valid access token, fetching a new one if none is cached or it's about to expire."""
    cached = _cache.get(request.cache_key)
    if cached and cached[1] - EXPIRY_MARGIN_SECONDS > time.monotonic():
        return cached[0]

    try:
        response = await client.post(
            request.token_url, data=request.form(),
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.HTTPError as e:
        raise OAuthError(f"Could not reach the token endpoint {request.token_url}: {e}") from e

    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400 or "access_token" not in body:
        reason = body.get("error_description") or body.get("error") or response.text[:200] or "no details"
        hint = ""
        if response.status_code in (400, 401):
            hint = (" Check the client ID and secret"
                    + (" and the user's password" if request.grant_type == "password" else "")
                    + ", and that the OAuth app is active.")
        raise OAuthError(f"The token request was refused ({response.status_code}: {reason}).{hint}")

    lifetime = body.get("expires_in")
    try:
        lifetime = float(lifetime)
    except (TypeError, ValueError):
        lifetime = DEFAULT_LIFETIME_SECONDS
    _cache[request.cache_key] = (body["access_token"], time.monotonic() + lifetime)
    return body["access_token"]
