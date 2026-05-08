"""
SyncState domain entity for gmail_zero_app.

Tracks the Gmail history ID high-water mark used for incremental sync.
One record is written per sync run; the most recent record is the active
sync state.  Older records are retained for diagnostic purposes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


class SyncType(StrEnum):
    """
    Discriminates between full and incremental sync runs.

    FULL:        All messages were re-fetched from Gmail from scratch.
                 Performed on first run or after IncrementalSyncError.
    INCREMENTAL: Only changes since the previous historyId were fetched.
                 Normal daily operation mode.
    """

    FULL = "full"
    INCREMENTAL = "incremental"


@dataclass(frozen=True)
class SyncState:
    """
    Immutable record of a completed sync operation.

    The sync pipeline writes a new SyncState at the end of every successful
    sync.  The SyncStateRepository exposes a ``latest()`` method that returns
    the most recent record.

    Attributes:
        id:              Auto-assigned database row ID.  None before persistence.
        history_id:      The Gmail historyId high-water mark at end of sync.
                         Passed to the next incremental sync as ``startHistoryId``.
        last_synced_at:  UTC datetime when this sync completed.
        sync_type:       FULL or INCREMENTAL.
        messages_synced: Number of messages created or updated in this sync run.
        created_at:      UTC datetime this record was created (equals last_synced_at
                         in practice; stored separately for auditability).
    """

    id: int | None
    history_id: str
    last_synced_at: datetime
    sync_type: SyncType
    messages_synced: int
    created_at: datetime

    @property
    def is_full_sync(self) -> bool:
        """True if this record represents a full sync."""
        return self.sync_type == SyncType.FULL

    @property
    def is_incremental_sync(self) -> bool:
        """True if this record represents an incremental sync."""
        return self.sync_type == SyncType.INCREMENTAL

    def __str__(self) -> str:
        return (
            f"SyncState(history_id={self.history_id!r}, "
            f"type={self.sync_type}, "
            f"messages={self.messages_synced}, "
            f"at={self.last_synced_at.isoformat()})"
        )
