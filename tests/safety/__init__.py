"""
Safety test suite for gmail_zero_app.

These tests prove the application CANNOT perform forbidden Gmail operations.
They must always pass.  A failure here means the safety architecture has
been compromised and must be treated as a security incident.

Tests implemented in Step 2 (SafetyGuard) and Step 4 (GmailClient whitelist).
This package marker is created in Step 1 so the suite can be referenced in
documentation and CI configuration from the start.
"""
