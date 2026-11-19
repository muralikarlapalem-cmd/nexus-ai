"""
NEXUS AI · app/core/security.py
JWT utilities — handles both:
  1. Keycloak-issued tokens (verified via JWKS endpoint) — Production
  2. Locally-issued tokens (HS256 fallback)              — Development / testing
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from jose import JWTError, jwt
from loguru import logger
from passlib.context import CryptContext

from app.config import settings

# ── Password hashing ──────────────────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── Local JWT (HS256 fallback) ────────────────────────────────

def create_access_token(subject: str | Any, extra_claims: dict | None = None) -> str:
    """
    Create a short-lived HS256 access token.
    `subject` is typically the user's UUID as a string.
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.jwt_access_token_expire_minutes)
    payload = {
        "sub": str(subject),
        "iat": now,
        "exp": expire,
        "type": "access",
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(subject: str | Any) -> str:
    now = datetime.now(timezone.utc)
    expire = now + timedelta(days=settings.jwt_refresh_token_expire_days)
    payload = {
        "sub": str(subject),
        "iat": now,
        "exp": expire,
        "type": "refresh",
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_local_token(token: str) -> dict:
    """Decode and validate a locally-issued HS256 token."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        return payload
    except JWTError as e:
        raise ValueError(f"Invalid token: {e}") from e


# ── Keycloak JWT (RS256 via JWKS) ─────────────────────────────
# The realm's public keys are cached. They're fetched again only when a token
# names a key we don't have (key rotation), at most every 30 seconds, so bad
# tokens can't make us hammer Keycloak.

_jwks_cache: dict | None = None
_jwks_fetched_at: float = 0.0
JWKS_REFRESH_MIN_SECONDS = 30


async def _fetch_jwks() -> dict:
    global _jwks_cache, _jwks_fetched_at
    import time

    async with httpx.AsyncClient() as client:
        resp = await client.get(str(settings.keycloak_jwks_url), timeout=5)
        resp.raise_for_status()
        _jwks_cache = resp.json()
        _jwks_fetched_at = time.monotonic()
        return _jwks_cache


def _has_key(kid: str | None) -> bool:
    return bool(_jwks_cache) and any(k.get("kid") == kid for k in _jwks_cache.get("keys", []))


async def decode_keycloak_token(token: str) -> dict:
    """
    Verify a Keycloak-issued JWT: signature (realm keys), expiry, issuer (the
    realm, by its browser or Docker address) and audience (nexus-backend).
    Raises ValueError on any validation failure.
    """
    import time

    from app.core.sso import accepted_issuers

    try:
        kid = jwt.get_unverified_header(token).get("kid")
        if _jwks_cache is None or (
            not _has_key(kid) and time.monotonic() - _jwks_fetched_at > JWKS_REFRESH_MIN_SECONDS
        ):
            logger.info("Fetching Keycloak public keys")
            await _fetch_jwks()
        payload = jwt.decode(
            token,
            _jwks_cache,
            algorithms=["RS256"],
            audience=settings.keycloak_client_id,
            options={"verify_iss": False},     # checked below: two addresses are valid
        )
    except httpx.HTTPError as e:
        raise ValueError(f"Could not reach Keycloak JWKS endpoint: {e}") from e
    except JWTError as e:
        raise ValueError(f"Invalid Keycloak token: {e}") from e

    if payload.get("iss") not in accepted_issuers():
        raise ValueError(f"Invalid Keycloak token: unexpected issuer {payload.get('iss')}")
    return payload


def token_algorithm(token: str) -> str | None:
    try:
        return jwt.get_unverified_header(token).get("alg")
    except JWTError:
        return None


async def decode_token(token: str) -> dict:
    """
    Verify a bearer token. NEXUS's own tokens (HS256) are checked locally;
    Keycloak's (RS256) against the realm. The token's own header says which,
    so a local sign-in never waits for Keycloak.
    Keycloak tokens come back with "_sso": True.
    """
    alg = token_algorithm(token)
    if alg == "RS256":
        payload = await decode_keycloak_token(token)
        payload["_sso"] = True
        return payload
    try:
        payload = decode_local_token(token)
    except ValueError as local_err:
        raise ValueError(f"Token invalid: {local_err}") from local_err
    payload.pop("_sso", None)
    return payload
