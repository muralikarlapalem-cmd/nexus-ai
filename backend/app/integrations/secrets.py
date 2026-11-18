"""
NEXUS AI · app/integrations/secrets.py
Connection credentials never go in the database. A connection's config stores
only the NAME of an environment variable (e.g. "INTEGRATION_SERVICENOW_PASSWORD"),
and the value is read from the environment when the connector runs.

Only variables starting with INTEGRATION_ can be referenced. Without that rule,
an admin could point a connection at their own server with password_env set to
OPENAI_API_KEY or JWT_SECRET_KEY, and NEXUS would send that secret to them.
"""

import os
import re

ALLOWED_PREFIX = "INTEGRATION_"
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class SecretError(ValueError):
    pass


def validate_env_var_name(name: str) -> str:
    if not name:
        raise SecretError("Environment variable name is empty")
    if not _NAME_RE.match(name):
        raise SecretError(f"'{name}' is not a valid environment variable name (use CAPITALS, digits and _)")
    if not name.startswith(ALLOWED_PREFIX):
        raise SecretError(f"Credential variables must start with {ALLOWED_PREFIX} (got '{name}')")
    return name


def is_set(name: str | None) -> bool:
    if not name:
        return False
    try:
        validate_env_var_name(name)
    except SecretError:
        return False
    return bool(os.environ.get(name))


def read_secret(name: str) -> str:
    validate_env_var_name(name)
    value = os.environ.get(name)
    if not value:
        raise SecretError(
            f"Environment variable {name} is not set. Add it to .env and restart the api and "
            f"celery-worker containers."
        )
    return value
