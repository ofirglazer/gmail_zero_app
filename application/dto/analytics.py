"""
Analytics DTOs for gmail_zero_app.

Plain frozen dataclasses used to carry computed analytics data from the
application layer to the presentation layer.  No business logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True)
class DashboardSummary:
    """
    All metrics needed to render the four goal-status cards on the dashboard.

    Produced by AnalyticsService.dashboard_summary() and passed directly
    to the Jinja2 template context.

    Attributes:
        inbox_count:                 Current message count in INBOX.
        inbox_size_bytes:            Total estimated size of INBOX messages.
        archive_unlabelled_count:    Archived messages with no custom label.
        sent_unresolved_count:       Sent messages with no workflow label.
        total_size_bytes:            Estimated total size of all messages.
        custom_label_coverage_pct:   Percentage of messages with a custom label (0-100).
        last_synced_at:              UTC datetime of most recent sync, or None if never synced.
        old_inbox_thread_count:      Inbox threads older than settings.old_thread_threshold_days.
    """

    inbox_count: int
    inbox_size_bytes: int
    archive_unlabelled_count: int
    sent_unresolved_count: int
    total_size_bytes: int
    custom_label_coverage_pct: float
    last_synced_at: datetime | None
    old_inbox_thread_count: int

    @property
    def inbox_zero_reached(self) -> bool:
        return self.inbox_count == 0

    @property
    def archive_zero_reached(self) -> bool:
        return self.archive_unlabelled_count == 0

    @property
    def sent_zero_reached(self) -> bool:
        return self.sent_unresolved_count == 0

    @property
    def total_size_gb(self) -> float:
        return round(self.total_size_bytes / (1024**3), 3)

    @property
    def inbox_size_mb(self) -> float:
        return round(self.inbox_size_bytes / (1024**2), 2)

    @property
    def has_ever_synced(self) -> bool:
        return self.last_synced_at is not None
