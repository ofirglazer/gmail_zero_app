"""
DailySnapshot domain entity for gmail_zero_app.

One snapshot is written at the end of each sync run.  Snapshots power
the daily progress graphs on the dashboard, showing trends toward each
of the four zero goals over a configurable time window (default 30 days).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date


@dataclass(frozen=True)
class DailySnapshot:
    """
    Immutable record of mailbox state captured at the end of a sync run.

    At most one snapshot is stored per calendar date.  If multiple syncs
    run on the same day, the most recent overwrites the earlier one —
    only end-of-day state is meaningful for trend analysis.

    Attributes:
        snapshot_date:             Calendar date of the snapshot (local date).
        inbox_count:               Total messages in INBOX.
        inbox_size_bytes:          Total size of all INBOX messages in bytes.
        archive_unlabelled_count:  Archived messages with no custom user label.
        sent_unresolved_count:     Sent messages not yet marked complete or
                                   labelled with a workflow label.
        total_size_bytes:          Total estimated size of all messages.
        custom_label_coverage_pct: Percentage of all messages that have at
                                   least one user-defined label (0.0-100.0).
    """

    snapshot_date: date
    inbox_count: int
    inbox_size_bytes: int
    archive_unlabelled_count: int
    sent_unresolved_count: int
    total_size_bytes: int
    custom_label_coverage_pct: float

    # ── Derived properties ────────────────────────────────────────────────────

    @property
    def total_size_mb(self) -> float:
        """Total mailbox size in megabytes."""
        return round(self.total_size_bytes / (1024 * 1024), 2)

    @property
    def total_size_gb(self) -> float:
        """Total mailbox size in gigabytes."""
        return round(self.total_size_bytes / (1024 * 1024 * 1024), 3)

    @property
    def inbox_size_mb(self) -> float:
        """Inbox size in megabytes."""
        return round(self.inbox_size_bytes / (1024 * 1024), 2)

    @property
    def archive_zero_reached(self) -> bool:
        """True if the Archive Zero goal is achieved for this snapshot."""
        return self.archive_unlabelled_count == 0

    @property
    def inbox_zero_reached(self) -> bool:
        """True if the Inbox Zero goal is achieved for this snapshot."""
        return self.inbox_count == 0

    @property
    def sent_zero_reached(self) -> bool:
        """True if the Sent Zero goal is achieved for this snapshot."""
        return self.sent_unresolved_count == 0

    def __str__(self) -> str:
        return (
            f"DailySnapshot("
            f"date={self.snapshot_date.isoformat()}, "
            f"inbox={self.inbox_count}, "
            f"archive_unlabelled={self.archive_unlabelled_count}, "
            f"total_size={self.total_size_gb}GB, "
            f"coverage={self.custom_label_coverage_pct:.1f}%)"
        )
