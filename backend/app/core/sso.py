"""
NEXUS AI · app/core/sso.py
Keycloak single sign-on.

  - Who is who: Keycloak realm roles nexus-admin / nexus-agent / nexus-analyst
    decide a signed-in user's NEXUS role (anyone else is a plain user).
  - Which tokens are trusted: RS256, signed by the realm's keys, issued by
    the realm (browser or Docker address), with "nexus-backend" in the audience.
  - Setup: ensure_realm() creates or repairs the realm, its roles and the web
    app's client through Keycloak's admin API; upsert_user() adds a user.
    Both are run from the CLI (python -m app.cli sso-setup / sso-user).
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from loguru import logger

from app.config import settings
from app.models.user import UserRole

ROLE_FOR_REALM_ROLE = {
    "nexus-admin": UserRole.ADMIN,
    "nexus-agent": UserRole.AGENT,
    "nexus-analyst": UserRole.ANALYST,
}
REALM_ROLE_FOR_ROLE = {v: k for k, v in ROLE_FOR_REALM_ROLE.items()}
ROLE_ORDER = [UserRole.ADMIN, UserRole.AGENT, UserRole.ANALYST, UserRole.USER]


def internal_url() -> str:
    return str(settings.keycloak_url).rstrip("/")


def public_url() -> str:
    return settings.keycloak_public_url.rstrip("/")


def accepted_issuers() -> set[str]:
    realm = settings.keycloak_realm
    return {f"{public_url()}/realms/{realm}", f"{internal_url()}/realms/{realm}"}


def role_from_token(payload: dict) -> UserRole:
    """Highest NEXUS role among the token's realm roles; USER if none."""
    roles = set((payload.get("realm_access") or {}).get("roles") or [])
    found = {ROLE_FOR_REALM_ROLE[r] for r in roles if r in ROLE_FOR_REALM_ROLE}
    for role in ROLE_ORDER:
        if role in found:
            return role
    return UserRole.USER


# ── Is SSO ready? (for the login page) ─────────────────────────
_status_cache: tuple[float, dict] | None = None


async def sso_status() -> dict[str, Any]:
    """
    What the login page needs: whether to show "Sign in with SSO", and where to go.
    Checks the realm exists (cached 30 s), so the button only appears when it works.
    """
    global _status_cache
    if _status_cache and time.monotonic() - _status_cache[0] < 30:
        return _status_cache[1]
    info: dict[str, Any] = {
        "enabled": False,
        "url": public_url(),
        "realm": settings.keycloak_realm,
        "client_id": settings.keycloak_frontend_client_id,
    }
    if not settings.sso_enabled:
        info["reason"] = "SSO is turned off (SSO_ENABLED=false)"
    else:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.get(f"{internal_url()}/realms/{settings.keycloak_realm}")
            if r.status_code == 200:
                info["enabled"] = True
            elif r.status_code == 404:
                info["reason"] = (f"Keycloak has no '{settings.keycloak_realm}' realm yet. "
                                  "Run: docker compose exec api python -m app.cli sso-setup")
            else:
                info["reason"] = f"Keycloak answered {r.status_code}"
        except httpx.HTTPError:
            info["reason"] = "Keycloak is not reachable"
    _status_cache = (time.monotonic(), info)
    return info


# ── Keycloak admin API (setup commands) ────────────────────────
class KeycloakAdmin:
    """Minimal client for the admin REST API, signed in as the master-realm admin."""

    def __init__(self, base_url: str | None = None, username: str | None = None,
                 password: str | None = None, transport: httpx.BaseTransport | None = None):
        self.base = (base_url or internal_url()).rstrip("/")
        self.username = username or settings.kc_admin
        self.password = password or settings.kc_admin_password
        if not self.username or not self.password:
            raise RuntimeError("KC_ADMIN and KC_ADMIN_PASSWORD must be set in .env")
        self.http = httpx.Client(timeout=20, transport=transport)
        r = self.http.post(f"{self.base}/realms/master/protocol/openid-connect/token", data={
            "grant_type": "password", "client_id": "admin-cli",
            "username": self.username, "password": self.password,
        })
        if r.status_code != 200:
            raise RuntimeError(f"Could not sign in to Keycloak as '{self.username}' ({r.status_code}). "
                               "Check KC_ADMIN / KC_ADMIN_PASSWORD in .env.")
        self.http.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    def _url(self, path: str) -> str:
        return f"{self.base}/admin/realms{path}"

    def get(self, path: str, **params):
        r = self.http.get(self._url(path), params=params)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: Any):
        r = self.http.post(self._url(path), json=body)
        r.raise_for_status()
        return r

    def put(self, path: str, body: Any):
        r = self.http.put(self._url(path), json=body)
        r.raise_for_status()
        return r

    def delete(self, path: str, body: Any = None):
        r = self.http.request("DELETE", self._url(path), json=body)
        r.raise_for_status()
        return r


def _frontend_client(app_url: str) -> dict:
    app_url = app_url.rstrip("/")
    return {
        "clientId": settings.keycloak_frontend_client_id,
        "name": "NEXUS AI web app",
        "enabled": True,
        "publicClient": True,                 # a browser app can't keep a secret: PKCE instead
        "standardFlowEnabled": True,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,   # no password grant through this client
        "serviceAccountsEnabled": False,
        "redirectUris": [f"{app_url}/*"],
        "webOrigins": [app_url],
        "rootUrl": app_url,
        "attributes": {
            "pkce.code.challenge.method": "S256",
            "post.logout.redirect.uris": f"{app_url}/*",
        },
        "protocolMappers": [{
            "name": "nexus-backend audience",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {
                "included.custom.audience": settings.keycloak_client_id,
                "access.token.claim": "true",
                "id.token.claim": "false",
            },
        }],
    }


def ensure_realm(admin: KeycloakAdmin, app_url: str | None = None) -> list[str]:
    """
    Create or repair the NEXUS realm: realm, roles, web-app client and its
    audience mapper. Safe to run any number of times. Returns what it did.
    """
    realm = settings.keycloak_realm
    app_url = app_url or settings.public_app_url
    done: list[str] = []

    if admin.get(f"/{realm}") is None:
        r = admin.http.post(f"{admin.base}/admin/realms", json={
            "realm": realm, "displayName": "NEXUS AI", "enabled": True,
            "registrationAllowed": False, "loginWithEmailAllowed": True,
            "bruteForceProtected": True, "accessTokenLifespan": 900,
            "ssoSessionIdleTimeout": 3600, "ssoSessionMaxLifespan": 36000,
        })
        r.raise_for_status()
        done.append(f"created realm '{realm}'")

    for name, role in ROLE_FOR_REALM_ROLE.items():
        if admin.get(f"/{realm}/roles/{name}") is None:
            admin.post(f"/{realm}/roles", {"name": name, "description": f"NEXUS AI {role.value}"})
            done.append(f"created role {name}")

    wanted = _frontend_client(app_url)
    found = admin.get(f"/{realm}/clients", clientId=wanted["clientId"]) or []
    if not found:
        admin.post(f"/{realm}/clients", wanted)
        done.append(f"created client {wanted['clientId']} (redirects to {app_url})")
    else:
        client = found[0]
        mappers = wanted.pop("protocolMappers")
        admin.put(f"/{realm}/clients/{client['id']}", {**client, **wanted,
                                                       "attributes": {**client.get("attributes", {}),
                                                                      **wanted["attributes"]}})
        existing = {m["name"] for m in (admin.get(f"/{realm}/clients/{client['id']}/protocol-mappers/models") or [])}
        for m in mappers:
            if m["name"] not in existing:
                admin.post(f"/{realm}/clients/{client['id']}/protocol-mappers/models", m)
                done.append(f"added mapper '{m['name']}'")
        done.append(f"updated client {wanted['clientId']} (redirects to {app_url})")
    return done


def upsert_user(admin: KeycloakAdmin, username: str, email: str, role: UserRole,
                password: str | None, full_name: str | None = None) -> str:
    """Create or update a Keycloak user with exactly one NEXUS role. Returns 'created' or 'updated'."""
    realm = settings.keycloak_realm
    first, _, last = (full_name or "").partition(" ")
    body = {"username": username, "email": email, "emailVerified": True, "enabled": True,
            "firstName": first or username, "lastName": last or ""}
    existing = admin.get(f"/{realm}/users", username=username, exact="true") or []
    if existing:
        user_id = existing[0]["id"]
        admin.put(f"/{realm}/users/{user_id}", body)
        outcome = "updated"
    else:
        r = admin.post(f"/{realm}/users", body)
        user_id = r.headers["Location"].rstrip("/").split("/")[-1]
        outcome = "created"

    if password:
        admin.put(f"/{realm}/users/{user_id}/reset-password",
                  {"type": "password", "value": password, "temporary": False})

    current = admin.get(f"/{realm}/users/{user_id}/role-mappings/realm") or []
    remove = [r for r in current if r["name"] in ROLE_FOR_REALM_ROLE]
    if remove:
        admin.delete(f"/{realm}/users/{user_id}/role-mappings/realm", remove)
    if role in REALM_ROLE_FOR_ROLE:
        role_rep = admin.get(f"/{realm}/roles/{REALM_ROLE_FOR_ROLE[role]}")
        admin.post(f"/{realm}/users/{user_id}/role-mappings/realm", [role_rep])
    logger.info(f"Keycloak user {username} {outcome} with role {role.value}")
    return outcome
