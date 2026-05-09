"""
Unit tests for config.settings and domain-layer constants.

Step 1 acceptance tests — these validate that the project skeleton is
correctly wired before any business logic is implemented.

All tests are marked @pytest.mark.unit.
"""

import pytest
from pydantic import ValidationError

from config.oauth_scopes import (
    FORBIDDEN_SCOPES,
    GMAIL_LABELS_SCOPE,
    GMAIL_READONLY_SCOPE,
    REQUIRED_SCOPES,
)
from config.settings import Environment, Settings
from domain.exceptions import (
    AuthenticationError,
    ConfigurationError,
    ForbiddenOperationError,
    GmailZeroError,
    IncrementalSyncError,
    LabelConfigError,
    LabelOperationError,
    SafetyViolationError,
    SyncError,
)
from domain.safety.constants import (
    ARCHIVE_TRIGGER_LABEL_ID,
    FORBIDDEN_API_OPERATIONS,
    MAX_BULK_OPERATION_MESSAGES,
    MAX_LABELS_PER_OPERATION,
    PROTECTED_ADD_LABEL_IDS,
    PROTECTED_LABEL_IDS,
)

# ── Settings: Environment enum ────────────────────────────────────────────────


@pytest.mark.unit
class TestEnvironmentEnum:
    """Environment is a StrEnum — members compare equal to their string values."""

    def test_demo_equals_string(self) -> None:
        assert Environment.DEMO == "demo"

    def test_production_equals_string(self) -> None:
        assert Environment.PRODUCTION == "production"

    def test_members_are_strings(self) -> None:
        assert isinstance(Environment.DEMO, str)
        assert isinstance(Environment.PRODUCTION, str)

    def test_from_string_demo(self) -> None:
        assert Environment("demo") is Environment.DEMO

    def test_from_string_production(self) -> None:
        assert Environment("production") is Environment.PRODUCTION


# ── Settings: valid construction ──────────────────────────────────────────────


@pytest.mark.unit
class TestSettingsConstruction:
    """Settings instantiates correctly with valid values."""

    def test_demo_settings_from_fixture(self, demo_settings: Settings) -> None:
        assert demo_settings.is_demo is True
        assert demo_settings.is_production is False

    def test_production_settings_from_fixture(
        self, production_settings: Settings
    ) -> None:
        assert production_settings.is_production is True
        assert production_settings.is_demo is False

    def test_db_url_contains_db_path(self, demo_settings: Settings) -> None:
        # db_path=":memory:" produces a valid SQLAlchemy URL for testing
        assert "sqlite:///" in demo_settings.db_url

    def test_defaults_are_sane(self) -> None:
        """Minimal Settings construction — only required fields overridden."""
        settings = Settings(
            env=Environment.DEMO,
            secret_key="a" * 32,
        )
        assert settings.port == 5000
        assert settings.host == "127.0.0.1"
        assert settings.sync_batch_size == 100
        assert settings.large_message_threshold_bytes == 5 * 1024 * 1024
        assert settings.very_large_message_threshold_bytes == 15 * 1024 * 1024
        assert settings.graph_history_days == 30
        assert settings.old_thread_threshold_days == 30
        assert settings.very_old_thread_threshold_days == 90


# ── Settings: host validator ──────────────────────────────────────────────────


@pytest.mark.unit
class TestSettingsHostValidator:
    """The host field must be 127.0.0.1 or localhost — never a network interface."""

    def test_localhost_ip_accepted(self) -> None:
        s = Settings(env=Environment.DEMO, secret_key="a" * 32, host="127.0.0.1")
        assert s.host == "127.0.0.1"

    def test_localhost_name_accepted(self) -> None:
        s = Settings(env=Environment.DEMO, secret_key="a" * 32, host="localhost")
        assert s.host == "localhost"

    def test_network_ip_rejected(self) -> None:
        with pytest.raises(ValidationError, match="local-only"):
            Settings(env=Environment.DEMO, secret_key="a" * 32, host="0.0.0.0")

    def test_external_ip_rejected(self) -> None:
        with pytest.raises(ValidationError, match="local-only"):
            Settings(env=Environment.DEMO, secret_key="a" * 32, host="192.168.1.100")


# ── Settings: threshold validator ────────────────────────────────────────────


@pytest.mark.unit
class TestSettingsSizeThresholds:
    """very_large threshold must exceed large threshold."""

    def test_valid_thresholds_accepted(self) -> None:
        s = Settings(
            env=Environment.DEMO,
            secret_key="a" * 32,
            large_message_threshold_bytes=3 * 1024 * 1024,
            very_large_message_threshold_bytes=20 * 1024 * 1024,
        )
        assert s.very_large_message_threshold_bytes > s.large_message_threshold_bytes

    def test_equal_thresholds_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings(
                env=Environment.DEMO,
                secret_key="a" * 32,
                large_message_threshold_bytes=5 * 1024 * 1024,
                very_large_message_threshold_bytes=5 * 1024 * 1024,
            )

    def test_inverted_thresholds_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings(
                env=Environment.DEMO,
                secret_key="a" * 32,
                large_message_threshold_bytes=15 * 1024 * 1024,
                very_large_message_threshold_bytes=5 * 1024 * 1024,
            )


# ## OAuth scope constants ##


@pytest.mark.unit
class TestOAuthScopeConstants:
    """Scope constants are correctly defined and mutually exclusive."""

    def test_required_scopes_contains_readonly(self) -> None:
        assert GMAIL_READONLY_SCOPE in REQUIRED_SCOPES

    def test_required_scopes_contains_labels(self) -> None:
        assert GMAIL_LABELS_SCOPE in REQUIRED_SCOPES

    def test_required_scopes_length(self) -> None:
        # Exactly two scopes — not more, not fewer.
        assert len(REQUIRED_SCOPES) == 2

    def test_no_required_scope_is_forbidden(self) -> None:
        """Required scopes must not overlap with forbidden scopes."""
        for scope in REQUIRED_SCOPES:
            assert scope not in FORBIDDEN_SCOPES, (
                f"Scope {scope!r} appears in both REQUIRED_SCOPES and "
                "FORBIDDEN_SCOPES — this is a safety configuration error."
            )

    def test_gmail_modify_is_forbidden(self) -> None:
        assert "https://www.googleapis.com/auth/gmail.modify" in FORBIDDEN_SCOPES

    def test_gmail_send_is_forbidden(self) -> None:
        assert "https://www.googleapis.com/auth/gmail.send" in FORBIDDEN_SCOPES

    def test_gmail_compose_is_forbidden(self) -> None:
        assert "https://www.googleapis.com/auth/gmail.compose" in FORBIDDEN_SCOPES

    def test_full_access_scope_is_forbidden(self) -> None:
        assert "https://mail.google.com/" in FORBIDDEN_SCOPES

    def test_forbidden_scopes_is_frozenset(self) -> None:
        """frozenset is immutable — cannot be accidentally mutated at runtime."""
        assert isinstance(FORBIDDEN_SCOPES, frozenset)


# ── Safety constants ──────────────────────────────────────────────────────────


@pytest.mark.unit
class TestSafetyConstants:
    """Safety constants are correctly defined and immutable."""

    def test_protected_label_ids_is_frozenset(self) -> None:
        assert isinstance(PROTECTED_LABEL_IDS, frozenset)

    def test_inbox_is_protected(self) -> None:
        assert "INBOX" in PROTECTED_LABEL_IDS

    def test_sent_is_protected(self) -> None:
        assert "SENT" in PROTECTED_LABEL_IDS

    def test_trash_is_protected(self) -> None:
        assert "TRASH" in PROTECTED_LABEL_IDS

    def test_spam_is_protected(self) -> None:
        assert "SPAM" in PROTECTED_LABEL_IDS

    def test_draft_is_protected(self) -> None:
        assert "DRAFT" in PROTECTED_LABEL_IDS

    def test_unread_is_protected(self) -> None:
        assert "UNREAD" in PROTECTED_LABEL_IDS

    def test_important_is_protected(self) -> None:
        assert "IMPORTANT" in PROTECTED_LABEL_IDS

    def test_archive_trigger_is_inbox(self) -> None:
        """The archive trigger must be INBOX — this is a critical safety invariant."""
        assert ARCHIVE_TRIGGER_LABEL_ID == "INBOX"

    def test_archive_trigger_is_in_protected_ids(self) -> None:
        assert ARCHIVE_TRIGGER_LABEL_ID in PROTECTED_LABEL_IDS

    def test_forbidden_api_operations_is_frozenset(self) -> None:
        assert isinstance(FORBIDDEN_API_OPERATIONS, frozenset)

    def test_send_is_forbidden_operation(self) -> None:
        assert "users.messages.send" in FORBIDDEN_API_OPERATIONS

    def test_delete_is_forbidden_operation(self) -> None:
        assert "users.messages.delete" in FORBIDDEN_API_OPERATIONS

    def test_trash_is_forbidden_operation(self) -> None:
        assert "users.messages.trash" in FORBIDDEN_API_OPERATIONS

    def test_untrash_is_forbidden_operation(self) -> None:
        assert "users.messages.untrash" in FORBIDDEN_API_OPERATIONS

    def test_draft_create_is_forbidden_operation(self) -> None:
        assert "users.drafts.create" in FORBIDDEN_API_OPERATIONS

    def test_draft_send_is_forbidden_operation(self) -> None:
        assert "users.drafts.send" in FORBIDDEN_API_OPERATIONS

    def test_batch_delete_is_forbidden_operation(self) -> None:
        assert "users.messages.batchDelete" in FORBIDDEN_API_OPERATIONS

    def test_batch_modify_is_forbidden_operation(self) -> None:
        assert "users.messages.batchModify" in FORBIDDEN_API_OPERATIONS

    def test_attachment_get_is_forbidden_operation(self) -> None:
        assert "users.messages.attachments.get" in FORBIDDEN_API_OPERATIONS

    def test_protected_add_label_ids_contains_trash(self) -> None:
        assert "TRASH" in PROTECTED_ADD_LABEL_IDS

    def test_protected_add_label_ids_contains_spam(self) -> None:
        assert "SPAM" in PROTECTED_ADD_LABEL_IDS

    def test_bulk_operation_limits_are_positive(self) -> None:
        assert MAX_BULK_OPERATION_MESSAGES > 0
        assert MAX_LABELS_PER_OPERATION > 0

    def test_bulk_operation_message_limit_is_reasonable(self) -> None:
        # High enough to be useful, low enough to limit blast radius.
        assert 1 <= MAX_BULK_OPERATION_MESSAGES <= 1000

    def test_bulk_labels_per_operation_limit_is_reasonable(self) -> None:
        assert 1 <= MAX_LABELS_PER_OPERATION <= 20


# ── Exception hierarchy ───────────────────────────────────────────────────────


@pytest.mark.unit
class TestExceptionHierarchy:
    """All custom exceptions inherit from GmailZeroError."""

    def test_forbidden_operation_error_is_gmail_zero_error(self) -> None:
        exc = ForbiddenOperationError("users.messages.send")
        assert isinstance(exc, GmailZeroError)

    def test_forbidden_operation_error_stores_operation(self) -> None:
        exc = ForbiddenOperationError("users.messages.trash")
        assert exc.operation == "users.messages.trash"

    def test_safety_violation_error_is_gmail_zero_error(self) -> None:
        exc = SafetyViolationError("Cannot remove INBOX label")
        assert isinstance(exc, GmailZeroError)

    def test_safety_violation_error_stores_reason(self) -> None:
        exc = SafetyViolationError("blocked", label_id="INBOX")
        assert exc.reason == "blocked"
        assert exc.label_id == "INBOX"

    def test_safety_violation_error_label_id_optional(self) -> None:
        exc = SafetyViolationError("bulk limit exceeded")
        assert exc.label_id is None

    def test_sync_error_is_gmail_zero_error(self) -> None:
        exc = SyncError("network failure")
        assert isinstance(exc, GmailZeroError)

    def test_incremental_sync_error_is_sync_error(self) -> None:
        exc = IncrementalSyncError("abc123")
        assert isinstance(exc, SyncError)
        assert exc.history_id == "abc123"

    def test_label_operation_error_is_gmail_zero_error(self) -> None:
        exc = LabelOperationError("add", "msg1", "Label_42", "not found")
        assert isinstance(exc, GmailZeroError)
        assert exc.operation == "add"
        assert exc.message_id == "msg1"
        assert exc.label_id == "Label_42"
        assert exc.reason == "not found"

    def test_configuration_error_is_gmail_zero_error(self) -> None:
        exc = ConfigurationError("bad config")
        assert isinstance(exc, GmailZeroError)

    def test_label_config_error_is_configuration_error(self) -> None:
        exc = LabelConfigError("/path/labels.toml", "missing key")
        assert isinstance(exc, ConfigurationError)
        assert exc.path == "/path/labels.toml"
        assert exc.reason == "missing key"

    def test_authentication_error_is_gmail_zero_error(self) -> None:
        exc = AuthenticationError("token expired")
        assert isinstance(exc, GmailZeroError)

    def test_all_exceptions_have_str_representation(self) -> None:
        """All exceptions should have informative string representations."""
        exceptions: list[Exception] = [
            ForbiddenOperationError("users.messages.send"),
            SafetyViolationError("test reason", "INBOX"),
            SyncError("sync failed"),
            IncrementalSyncError("history123"),
            LabelOperationError("remove", "msgid", "labelid", "api error"),
            ConfigurationError("bad config"),
            LabelConfigError("labels.toml", "parse error"),
            AuthenticationError("oauth failed"),
        ]
        for exc in exceptions:
            assert str(exc), f"{type(exc).__name__} has empty str() representation"
