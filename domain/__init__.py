"""
Domain layer for gmail_zero_app.

Contains pure domain models, exceptions, and safety constants.
This package has NO dependencies on infrastructure (database, Gmail API,
HTTP frameworks) — only Python stdlib and dataclasses.

Sub-packages:
    models/   — Domain entity dataclasses (Message, Label, Thread, etc.)
    safety/   — SafetyGuard and immutable safety constants
"""