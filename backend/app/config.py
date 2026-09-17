"""
NEXUS AI · app/config.py
Centralised settings loaded from environment / .env file.
All downstream modules import `settings` from here — never os.getenv directly.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(
            Path(__file__).resolve().parents[2] / ".env",
            Path(__file__).resolve().parents[2] / ".env.local"
        ),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────────
    app_env: Literal["development", "staging", "production"] = "development"
    app_name: str = "NEXUS AI"
    app_version: str = "0.1.0"
    debug: bool = False
    log_level: str = "INFO"

    # ── Database ─────────────────────────────────────────────
    database_url: str  # postgresql+asyncpg://...
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "nexusai"
    postgres_user: str = "nexus"
    postgres_password: str

    # ── Redis ────────────────────────────────────────────────
    redis_url: str
    celery_broker_url: str
    celery_result_backend: str

    # ── Keycloak ─────────────────────────────────────────────
    keycloak_url: AnyHttpUrl
    keycloak_realm: str = "nexus"
    keycloak_client_id: str = "nexus-backend"
    keycloak_client_secret: str
    keycloak_jwks_url: AnyHttpUrl  # for JWT public-key verification

    # ── JWT (local fallback) ──────────────────────────────────
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_days: int = 7

    # ── LLM (Phase 3) ─────────────────────────────────────────
    openai_api_key: str = ""
    llm_model: str = "gpt-4o"
    llm_temperature: float = 0.0

    # ── LangSmith (Phase 5) ───────────────────────────────────
    langsmith_api_key: str = ""
    langsmith_project: str = "nexus-ai"
    langchain_tracing_v2: bool = False

    # ── Qdrant (Phase 2) ──────────────────────────────────────
    qdrant_host: str = "qdrant"
    qdrant_port: int = 6333
    qdrant_collection: str = "nexus_knowledge"

    # ── Security ─────────────────────────────────────────────
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]
    rate_limit_per_minute: int = 60

    # ── Derived helpers ───────────────────────────────────────
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v):
        """Accept JSON string or list."""
        if isinstance(v, str):
            import json
            return json.loads(v)
        return v

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        if self.is_production:
            assert self.jwt_secret_key != "REPLACE_ME_WITH_A_STRONG_RANDOM_SECRET_32_CHARS_MIN", \
                "Set a real JWT_SECRET_KEY in production"
            assert len(self.jwt_secret_key) >= 32, \
                "JWT_SECRET_KEY must be at least 32 characters in production"
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton — call this everywhere."""
    return Settings()


# Convenient module-level shorthand
settings = get_settings()
