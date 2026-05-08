"""
Immutable safety constants for gmail_zero_app.

These constants define the hard safety boundaries of the entire application.
They are referenced by:

    - ``domain.safety.guard.SafetyGuard``    — validates label operations
    - ``infrastructure.gmail.client.GmailClient`` — whitelist enforcement
    - ``config.oauth_scopes``                — scope validation
    - ``tests.safety``                       — the mandatory safety test suite

PLACEMENT IN DOMAIN LAYER:
    These constants live in the domain layer (zero infrastructure dependencies)
    so they can be imported by the SafetyGuard, the GmailClient, and the test
    suite without creating circular dependencies or requiring a running Flask
    application.

⚠️  MODIFYING THIS FILE IS A SECURITY ACTION.
    Any change to PROTECTED_LABEL_IDS or FORBIDDEN_API_OPERATIONS must be
    reviewed against the safety model documented in THREAT_MODEL.md and must
    be accompanied by updated tests in tests/safety/.
"""

# ── Gmail system label IDs ────────────────────────────────────────────────────
#
# These are Gmail's internal label identifiers.  They are constant across
# all personal Gmail accounts (they are not user-generated IDs).
#
# PROTECTED_LABEL_IDS: labels that may NEVER be removed from a message by
# this application.  Removing INBOX archives the message (forbidden).
# Removing SENT, DRAFT, etc. would corrupt Gmail's internal categorisation.
#
PROTECTED_LABEL_IDS: frozenset[str] = frozenset({
    "INBOX",
    "SENT",
    "DRAFT",
    "TRASH",
    "SPAM",
    "STARRED",
    "IMPORTANT",
    "UNREAD",
    "CATEGORY_PERSONAL",
    "CATEGORY_SOCIAL",
    "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
})

# Labels that may not be ADDED to a message by this application.
# TRASH and SPAM assignment should only be done by Gmail itself.
# DRAFT and SENT are internal state markers not meant for user assignment.
#
PROTECTED_ADD_LABEL_IDS: frozenset[str] = frozenset({
    "TRASH",
    "SPAM",
    "DRAFT",
    "SENT",
})

# ── Archive trigger ───────────────────────────────────────────────────────────
#
# Removing the INBOX label from a message is equivalent to archiving it.
# This is an archive operation and is explicitly forbidden.
# The SafetyGuard checks every removeLabelIds list against this value.
#
ARCHIVE_TRIGGER_LABEL_ID: str = "INBOX"

# ── Forbidden Gmail API operation names ───────────────────────────────────────
#
# These are the exact resource.method names from the Gmail API discovery
# document.  The GmailClient checks every intended API call against this set
# before execution.  If a method name appears here, the call raises
# ForbiddenOperationError regardless of arguments or caller context.
#
# No exceptions.  No bypass mechanism.
#
FORBIDDEN_API_OPERATIONS: frozenset[str] = frozenset({
    # ── Sending ───────────────────────────────────────────────────────────────
    "users.messages.send",

    # ── Draft management ──────────────────────────────────────────────────────
    "users.drafts.create",
    "users.drafts.update",
    "users.drafts.delete",
    "users.drafts.send",
    "users.drafts.list",    # not needed; included to minimise attack surface

    # ── Destructive message operations ────────────────────────────────────────
    "users.messages.delete",
    "users.messages.trash",
    "users.messages.untrash",

    # ── Batch operations (too broad; cannot pre-validate safety) ──────────────
    "users.messages.batchDelete",
    "users.messages.batchModify",

    # ── Message creation (import/insert creates new messages) ─────────────────
    "users.messages.import",
    "users.messages.insert",

    # ── Attachment access (not needed; metadata-only operation) ───────────────
    "users.messages.attachments.get",
})

# ── Bulk operation limits ─────────────────────────────────────────────────────
#
# Conservative limits on the blast radius of bulk label operations.
# These are safety limits, not Gmail API limits.
#
# A single bulk operation may not affect more than MAX_BULK_OPERATION_MESSAGES
# messages, and may not add/remove more than MAX_LABELS_PER_OPERATION labels
# per message.  The SafetyGuard enforces these before any API call.
#
MAX_BULK_OPERATION_MESSAGES: int = 500
MAX_LABELS_PER_OPERATION: int = 10