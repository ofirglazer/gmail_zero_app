"""
Persistence sub-package for gmail_zero_app.

Entry points:
    from infrastructure.persistence.database import build_engine, get_session, initialise_db
    from infrastructure.persistence.repositories import MessageRepository, LabelRepository
    from infrastructure.persistence.repositories import SyncStateRepository, SnapshotRepository
"""

from infrastructure.persistence.database import build_engine, get_session, initialise_db

__all__ = ["build_engine", "get_session", "initialise_db"]
