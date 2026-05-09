"""
Application settings for gmail_zero_app.

All configuration is loaded from environment variables (prefixed GMAIL_ZERO_)
and optionally from a .env file at the project root.  Pydantic-settings
handles type coercion, validation, and missing-value errors automatically.

Usage:
    from config import get_settings

    settings = get_settings()
    if settings.is_demo:
        ...

Security note:
    The host validator enforces that Flask binds only to localhost.
    This application is designed to run locally only and must never be
    exposed to a network interface.
"""

from enum import StrEnum
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    """
    Runtime environment modes.

    PRODUCTION: Connect to the real Gmail API using OAuth credentials.
    DEMO:       Use the MockGmailClient with synthetic data — no credentials
                required.  Safe for demonstrations and testing.
    """

    PRODUCTION = "production"
    DEMO = "demo"


class Settings(BaseSettings):
    """
    Central application configuration.

    All fields correspond to environment variables prefixed with GMAIL_ZERO_.
    Example: GMAIL_ZERO_PORT=8080 sets the `port` field to 8080.

    Pydantic validates types and constraints at instantiation time, so
    misconfiguration fails loudly at startup rather than silently at runtime.
    """

    model_config = SettingsConfigDict(
        env_prefix="GMAIL_ZERO_",
        env_file=".env",
        env_file_encoding="utf-8",
        # Do not raise on extra env vars — the host environment may have
        # unrelated GMAIL_ZERO_* variables.
        extra="ignore",
        case_sensitive=False,
    )

    # ── Runtime environment ───────────────────────────────────────────────────

    env: Environment = Environment.DEMO

    # ── File paths ────────────────────────────────────────────────────────────
    # All paths default to locations relative to the project root.
    # Override with absolute paths in .env if the app is run from elsewhere.

    db_path: Path = Path("data/gmail_zero_app.db")
    credentials_path: Path = Path("data/credentials/credentials.json")
    token_path: Path = Path("data/credentials/token.json")
    labels_config_path: Path = Path("config/labels.toml")

    # ── Flask ─────────────────────────────────────────────────────────────────

    # Host is validated below — must be 127.0.0.1 or localhost.
    host: str = "127.0.0.1"
    port: int = Field(default=5000, ge=1024, le=65535)

    # Must be overridden in production .env with a cryptographically random value.
    # Generate with: python -c "import secrets; print(secrets.token_hex(32))"
    secret_key: str = Field(
        default="dev-insecure-key-MUST-override-in-production",
        min_length=16,
        description=(
            "Flask secret key for session signing and CSRF tokens. "
            "Override with a strong random value in production .env."
        ),
    )

    # Never enable debug mode in production — it exposes an interactive
    # debugger that can execute arbitrary code.
    debug: bool = False

    # ── Sync behaviour ────────────────────────────────────────────────────────

    # Number of message IDs processed per API batch during full sync.
    # Gmail API batchGet equivalent is sequential gets; this controls
    # how many we process before committing to DB.
    sync_batch_size: int = Field(default=100, ge=1, le=500)

    # Milliseconds to pause between API batch calls to respect rate limits.
    # Gmail quota: 250 units/user/second. Each messages.get costs 5 units.
    sync_rate_limit_delay_ms: int = Field(default=50, ge=0, le=5000)

    # ── Size thresholds (bytes) ───────────────────────────────────────────────
    # Messages above these sizes are flagged in the Size Reduction workflow.

    large_message_threshold_bytes: int = Field(
        default=5 * 1024 * 1024,  # 5 MB
        ge=1024 * 1024,  # minimum 1 MB
        description="Byte size above which a message is flagged as 'Large'.",
    )
    very_large_message_threshold_bytes: int = Field(
        default=15 * 1024 * 1024,  # 15 MB
        ge=1024 * 1024,
        description="Byte size above which a message is flagged as 'Very Large'.",
    )

    # ── Dashboard ─────────────────────────────────────────────────────────────

    # Days of history shown on progress graphs (30 or 90 day toggle in UI).
    graph_history_days: int = Field(default=30, ge=7, le=365)

    # Threads older than these thresholds (days) appear in the unresolved list.
    old_thread_threshold_days: int = Field(default=30, ge=1)
    very_old_thread_threshold_days: int = Field(default=90, ge=1)

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("host")
    @classmethod
    def host_must_be_localhost(cls, value: str) -> str:
        """
        Enforce local-only binding.

        This application must never be bound to a network interface.
        If a non-localhost host is configured, fail loudly at startup.
        """
        allowed = {"127.0.0.1", "localhost"}
        if value not in allowed:
            raise ValueError(
                f"GMAIL_ZERO_HOST must be one of {allowed} (local-only application). Got: {value!r}"
            )
        return value

    @field_validator("very_large_message_threshold_bytes")
    @classmethod
    def very_large_must_exceed_large(cls, value: int, info: object) -> int:
        """Ensure the 'very large' threshold is strictly above the 'large' threshold."""
        # Access sibling field via info.data (pydantic v2 style)
        data = getattr(info, "data", {})
        large = data.get("large_message_threshold_bytes", 0)
        if large and value <= large:
            raise ValueError(
                f"very_large_message_threshold_bytes ({value}) must exceed "
                f"large_message_threshold_bytes ({large})."
            )
        return value

    # ── Derived properties ────────────────────────────────────────────────────

    @property
    def is_demo(self) -> bool:
        """True when running in demo mode with MockGmailClient."""
        return self.env == Environment.DEMO

    @property
    def is_production(self) -> bool:
        """True when running against the real Gmail API."""
        return self.env == Environment.PRODUCTION

    @property
    def db_url(self) -> str:
        """SQLAlchemy database URL for the configured SQLite file."""
        return f"sqlite:///{self.db_path.resolve()}"


def get_settings() -> Settings:
    """
    Construct and return the application settings.

    This is the single entry point for settings access throughout the
    application.  The Flask app factory calls this once and stores the
    result on ``app.config``; all services receive settings via dependency
    injection rather than calling this function directly.

    Tests should instantiate ``Settings()`` directly with explicit field
    values rather than relying on environment variables.

    Returns:
        A fully validated ``Settings`` instance.

    Raises:
        pydantic.ValidationError: If any required setting is missing or
            fails validation (e.g., host is not localhost).
    """
    return Settings()
