"""
NEXUS AI · app/integrations/mapping.py
Config-driven value mapping between a source system and NEXUS.

Each connector ships sensible defaults. A connection can override any of them
in its config, without code changes:

    "mappings": {
        "priority": {"1": "critical", "2": "high", "3": "medium", "4": "low", "5": "low"},
        "category": {"inquiry": "other", "db": "database"},
        "status":   {"1": "open", "2": "in_progress", "6": "resolved"}
    }

Unknown source values fall back to the field's default ("medium", "other",
"open"). Lookups ignore case and surrounding spaces.
"""

from typing import Any

FIELD_DEFAULTS: dict[str, str] = {
    "priority": "medium",
    "category": "other",
    "status": "open",
}

ALLOWED_VALUES: dict[str, set[str]] = {
    "priority": {"low", "medium", "high", "critical"},
    "category": {"network", "hardware", "software", "security", "access",
                 "performance", "database", "other"},
    "status": {"open", "triaged", "in_progress", "awaiting_user",
               "resolved", "closed", "cancelled"},
}


def _norm(value: Any) -> str:
    return str(value).strip().lower()


class ValueMapper:
    """Translates source values to NEXUS values (inbound) and back (outbound)."""

    def __init__(self, defaults: dict[str, dict[str, str]], overrides: dict[str, dict[str, str]] | None = None):
        merged: dict[str, dict[str, str]] = {}
        for field in FIELD_DEFAULTS:
            table = {**defaults.get(field, {}), **(overrides or {}).get(field, {})}
            merged[field] = {_norm(k): _norm(v) for k, v in table.items()}
        self._inbound = merged
        self._validate()

    def _validate(self) -> None:
        for field, table in self._inbound.items():
            bad = {v for v in table.values() if v not in ALLOWED_VALUES[field]}
            if bad:
                raise ValueError(
                    f"Mapping for '{field}' uses values NEXUS doesn't know: {sorted(bad)}. "
                    f"Allowed: {sorted(ALLOWED_VALUES[field])}"
                )

    def to_nexus(self, field: str, source_value: Any) -> str:
        """Source value → NEXUS value, e.g. to_nexus('priority', '1') == 'critical'."""
        if source_value is None or source_value == "":
            return FIELD_DEFAULTS[field]
        key = _norm(source_value)
        if key in self._inbound[field]:
            return self._inbound[field][key]
        # Already a NEXUS value (e.g. a REST source that uses the same words)
        if key in ALLOWED_VALUES[field]:
            return key
        return FIELD_DEFAULTS[field]

    def to_source(self, field: str, nexus_value: str) -> str | None:
        """NEXUS value → the first source value that maps to it (used for write-back)."""
        target = _norm(nexus_value)
        for source_value, mapped in self._inbound[field].items():
            if mapped == target:
                return source_value
        return None

    def as_dict(self) -> dict[str, dict[str, str]]:
        return {field: dict(table) for field, table in self._inbound.items()}
