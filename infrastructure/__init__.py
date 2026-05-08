"""
Infrastructure layer for gmail_zero_app.

All I/O concerns live here: Gmail API client, SQLite persistence, OAuth flow,
and the optional APScheduler wrapper.  Nothing in this package is imported by
the domain layer — dependencies point inward only.

Sub-packages:
    gmail/         — GmailClient, MockGmailClient, OAuth flow, entity mapper
    persistence/   — SQLAlchemy ORM models, database engine, repositories
    scheduler/     — Optional APScheduler sync job wrapper
"""
