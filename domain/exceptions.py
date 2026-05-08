"""
Exception hierarchy for gmail_zero_app.

All application-specific exceptions inherit from ``GmailZeroError``,
providing a single catch-all for unexpected failures while allowing
targeted handling of specific error categories.

Hierarchy:
    GmailZeroError                     Base for all app exceptions
    ├── ForbiddenOperationError         GmailClient whitelist violation
    ├── SafetyViolationError            SafetyGuard rule violation
    ├── SyncError                       Sync pipeline failure
    │   └── IncrementalSyncError        Requires full resync
    ├── LabelOperationError             API-level label failure
    ├── ConfigurationError              Misconfiguration at startup
    │   └── LabelConfigError            labels.toml parse/validation failure
    └── AuthenticationError             OAuth flow failure

Design note:
    ForbiddenOperationError is a programming error — it should never be
    caught and suppressed.  It means code tried to call a Gmail API
    operation that is architecturally forbidden.

    SafetyViolationError is a user/input error — it is safe to catch
    at the route level and return an HTTP 400 response.
"""


class GmailZeroError(Exception):
    """Base exception for all gmail_zero_app errors."""


# ── Safety exceptions ─────────────────────────────────────────────────────────


class ForbiddenOperationError(GmailZeroError):
    """
    Raised when code attempts to call a Gmail API method not on the whitelist.

    This represents a programming error — a developer has introduced code
    that attempts to use a forbidden API operation (send, delete, trash, etc.).
    It should propagate to the top level and never be silently caught.

    Attributes:
        operation: The forbidden API method name that was attempted.
    """

    def __init__(self, operation: str) -> None:
        self.operation = operation
        super().__init__(
            f"Forbidden Gmail operation attempted: {operation!r}. "
            "This operation is not on the GmailClient whitelist. "
            "Review the safety architecture before adding new API calls."
        )


class SafetyViolationError(GmailZeroError):
    """
    Raised by SafetyGuard when a label operation violates a safety rule.

    Unlike ForbiddenOperationError (programming error), this can arise
    from user input — for example, the UI somehow constructing a request
    to remove the INBOX label.  It is safe to catch at the HTTP boundary
    and return an HTTP 400 response with the reason message.

    Attributes:
        reason:   Human-readable description of the violated rule.
        label_id: The Gmail label ID involved (if applicable).
    """

    def __init__(self, reason: str, label_id: str | None = None) -> None:
        self.reason = reason
        self.label_id = label_id
        label_info = f" (label: {label_id!r})" if label_id else ""
        super().__init__(f"Safety rule violation{label_info}: {reason}")


# ── Sync exceptions ───────────────────────────────────────────────────────────


class SyncError(GmailZeroError):
    """Raised when the sync pipeline encounters an unrecoverable error."""


class IncrementalSyncError(SyncError):
    """
    Raised when incremental sync cannot proceed and a full resync is needed.

    Typically occurs when the ``historyId`` stored locally is too old —
    Gmail's History API only retains history for approximately one week.
    The SyncService should catch this and schedule a full sync.

    Attributes:
        history_id: The stale historyId that caused the failure.
    """

    def __init__(self, history_id: str) -> None:
        self.history_id = history_id
        super().__init__(
            f"Incremental sync failed: historyId {history_id!r} is no longer "
            "valid (Gmail history may have expired). A full resync is required."
        )


# ── Label exceptions ──────────────────────────────────────────────────────────


class LabelOperationError(GmailZeroError):
    """
    Raised when a label add or remove operation fails at the Gmail API level.

    Attributes:
        operation:  'add' or 'remove'.
        message_id: The Gmail message ID the operation was attempted on.
        label_id:   The Gmail label ID being added or removed.
        reason:     The underlying error description.
    """

    def __init__(
        self,
        operation: str,
        message_id: str,
        label_id: str,
        reason: str,
    ) -> None:
        self.operation = operation
        self.message_id = message_id
        self.label_id = label_id
        self.reason = reason
        super().__init__(
            f"Label {operation!r} failed — "
            f"message: {message_id!r}, label: {label_id!r}. "
            f"Reason: {reason}"
        )


# ── Configuration exceptions ──────────────────────────────────────────────────


class ConfigurationError(GmailZeroError):
    """
    Raised when the application detects a configuration problem at startup.

    Configuration errors are always fatal — the app should not start with
    a known misconfiguration.
    """


class LabelConfigError(ConfigurationError):
    """
    Raised when ``labels.toml`` cannot be parsed or fails semantic validation.

    Attributes:
        path:   Filesystem path to the offending config file.
        reason: Description of the parse or validation failure.
    """

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(
            f"Invalid label configuration at {path!r}: {reason}. "
            "Fix the error in labels.toml and restart the application."
        )


# ── Auth exceptions ───────────────────────────────────────────────────────────


class AuthenticationError(GmailZeroError):
    """
    Raised when OAuth token acquisition, refresh, or scope validation fails.

    This is raised at startup if:
    - No credentials.json is found (first-time setup incomplete).
    - The stored token.json contains forbidden scopes.
    - Token refresh fails and interactive re-authorisation is not possible
      (e.g., running in a headless environment).
    """