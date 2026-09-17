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
# We cache the JWKS in memory; they are refreshed when a kid mismatch occurs.

_jwks_cache: dict | None = None


async def _fetch_jwks() -> dict:
    global _jwks_cache
    async with httpx.AsyncClient() as client:
        resp = await client.get(str(settings.keycloak_jwks_url), timeout=10)
        resp.raise_for_status()
        _jwks_cache = resp.json()
        return _jwks_cache


async def decode_keycloak_token(token: str) -> dict:
    """
    Verify a Keycloak-issued JWT using the realm's public JWKS.
    Raises ValueError on any validation failure.
    """
    global _jwks_cache

    try:
        # Attempt decode with cached keys first
        if _jwks_cache is None:
            _jwks_cache = await _fetch_jwks()

        try:
            payload = jwt.decode(
                token,
                _jwks_cache,
                algorithms=["RS256"],
                audience=settings.keycloak_client_id,
            )
            return payload
        except JWTError:
            # Refresh JWKS (key rotation) and retry once
            logger.info("JWKS cache miss — refreshing Keycloak public keys")
            _jwks_cache = await _fetch_jwks()
            payload = jwt.decode(
                token,
                _jwks_cache,
                algorithms=["RS256"],
                audience=settings.keycloak_client_id,
            )
            return payload

    except httpx.HTTPError as e:
        raise ValueError(f"Could not reach Keycloak JWKS endpoint: {e}") from e
    except JWTError as e:
        raise ValueError(f"Invalid Keycloak token: {e}") from e


async def decode_token(token: str) -> dict:
    """
    Universal decoder — tries Keycloak first, falls back to local HS256.
    This allows the API to work in dev without Keycloak running.
    """
    try:
        return await decode_keycloak_token(token)
    except Exception as kc_err:
        logger.debug(f"Keycloak decode failed ({kc_err}), trying local JWT")
        try:
            return decode_local_token(token)
        except ValueError as local_err:
            raise ValueError(f"Token invalid: {local_err}") from local_err
