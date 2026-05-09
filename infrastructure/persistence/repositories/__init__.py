"""
Repository implementations for gmail_zero_app.

All repositories accept a SQLAlchemy Session via constructor injection
and return domain entities (frozen dataclasses), never ORM objects.

    from infrastructure.persistence.repositories import (
        MessageRepository,
        LabelRepository,
        SyncStateRepository,
        SnapshotRepository,
    )
"""

from infrastructure.persistence.repositories.label_repository import LabelRepository
from infrastructure.persistence.repositories.message_repository import (
    MessageFilter,
    MessageRepository,
    SenderStats,
)
from infrastructure.persistence.repositories.snapshot_repository import SnapshotRepository
from infrastructure.persistence.repositories.sync_state_repository import SyncStateRepository

__all__ = [
    "LabelRepository",
    "MessageFilter",
    "MessageRepository",
    "SenderStats",
    "SnapshotRepository",
    "SyncStateRepository",
]
