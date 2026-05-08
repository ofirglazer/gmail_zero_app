"""
Gmail OAuth 2.0 scope constants for gmail_zero_app.

These constants define the exact OAuth scopes this application will request
and the scopes that are explicitly forbidden.  They are the authoritative
source of truth for scope configuration — no other file should define or
hard-code Gmail scope strings.

SECURITY:
    Never add gmail.modify, gmail.compose, gmail.send, or the full-access
    scope (mail.google.com) to REQUIRED_SCOPES.  Doing so would undermine
    the safety-by-design architecture and must be treated as a security
    incident, not a feature addition.

    The FORBIDDEN_SCOPES set is checked by the OAuth module at token load
    time.  If a stored token contains a forbidden scope (e.g., from a
    previous broader authorisation), the app refuses to start and forces
    re-authorisation.
"""

# ── Allowed scopes ────────────────────────────────────────────────────────────

# Read-only access to message metadata, threads, labels, and history.
# Does NOT grant write access to messages.
GMAIL_READONLY_SCOPE: str = "https://www.googleapis.com/auth/gmail.readonly"

# Create, update, delete labels; add/remove labels on messages.
# This is the narrowest scope that enables the label management workflow.
# Note: gmail.labels implies users.messages.modify for label fields only,
# but the SafetyGuard layer restricts which labels may be modified.
GMAIL_LABELS_SCOPE: str = "https://www.googleapis.com/auth/gmail.labels"

# The complete set of scopes to request during OAuth authorisation.
# This is passed verbatim to google-auth-oauthlib's flow.
REQUIRED_SCOPES: list[str] = [
    GMAIL_READONLY_SCOPE,
    GMAIL_LABELS_SCOPE,
]

# ── Forbidden scopes ──────────────────────────────────────────────────────────

# Any token carrying these scopes grants powers beyond this app's mandate.
# The OAuth module checks stored tokens against this set at startup.
FORBIDDEN_SCOPES: frozenset[str] = frozenset({
    # Full modify access — allows archiving, moving, marking read, etc.
    "https://www.googleapis.com/auth/gmail.modify",
    # Compose/draft access — allows creating and editing drafts.
    "https://www.googleapis.com/auth/gmail.compose",
    # Send access — allows sending emails.
    "https://www.googleapis.com/auth/gmail.send",
    # Full mailbox access — supersedes all other scopes.
    "https://mail.google.com/",
})