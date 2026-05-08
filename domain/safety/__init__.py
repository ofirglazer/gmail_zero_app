"""
Safety sub-package for gmail_zero_app.

Contains the immutable safety constants and the SafetyGuard domain service.
This package is intentionally dependency-free — it imports nothing from
infrastructure, making it trivially testable and immune to framework changes.

Contents:
    constants.py  — PROTECTED_LABEL_IDS, FORBIDDEN_API_OPERATIONS, etc.
    guard.py      — SafetyGuard (implemented in Step 2)
"""