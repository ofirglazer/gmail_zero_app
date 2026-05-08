"""
Root pytest configuration and shared fixtures for gmail_zero_app.

Fixtures defined here are available to all test modules without import.
Step-specific fixtures are defined in their own conftest.py files within
the relevant sub-package.

Marker registration is in pyproject.toml [tool.pytest.ini_options].
"""

import pytest

from config.settings import Environment, Settings

# ── Settings fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def demo_settings() -> Settings:
    """
    Settings instance configured for demo/test use.

    Uses an in-memory SQLite database so tests never touch the real DB file.
    All paths are set to values that do not require real files to exist.
    """
    return Settings(
        env=Environment.DEMO,
        secret_key="test-secret-key-32-chars-minimum!",
        db_path=":memory:",  # type: ignore[arg-type]
        host="127.0.0.1",
        port=5000,
        debug=False,
        sync_batch_size=10,
        sync_rate_limit_delay_ms=0,  # No delay in tests
    )


@pytest.fixture
def production_settings(tmp_path: pytest.fixture) -> Settings:  # type: ignore[valid-type]
    """
    Settings instance configured for production mode, with paths pointing
    to a temporary directory so tests remain isolated.
    """
    credentials_dir = tmp_path / "credentials"
    credentials_dir.mkdir()
    return Settings(
        env=Environment.PRODUCTION,
        secret_key="test-secret-key-32-chars-minimum!",
        db_path=tmp_path / "test.db",
        credentials_path=credentials_dir / "credentials.json",
        token_path=credentials_dir / "token.json",
        host="127.0.0.1",
        port=5000,
        debug=False,
    )
