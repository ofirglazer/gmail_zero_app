"""
Flask route blueprints for gmail_zero_app.

Each module defines one Blueprint registered by the app factory.

Blueprint map:
    dashboard  — / (command centre, progress graphs)
    inbox      — /inbox
    archive    — /archive
    sent       — /sent
    size       — /size
    labels     — /labels
    search     — /search
    sync       — /sync  (POST — triggers manual sync)
    settings   — /settings
    api        — /api/v1 (JSON endpoints for Chart.js graphs)

Implemented in Steps 6-8.
"""
