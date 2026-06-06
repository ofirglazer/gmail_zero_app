"""
AnalyticsService — assembles dashboard metrics from repository queries.

This service is intentionally thin.  It adds no business logic beyond
delegating to repository methods and assembling the results into typed DTOs.
All heavy lifting (SQL aggregation, pagination, sorting) lives in the repos.

Design note:
    AnalyticsService is a read-only service.  It never mutates state.
    Constructor receives all repositories via DI; no service locators.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from application.dto.analytics import DashboardSummary
from infrastructure.persistence.repositories.message_repository import MessageFilter

if TYPE_CHECKING:
    from config.settings import Settings
    from domain.models.daily_snapshot import DailySnapshot
    from domain.models.thread import Thread
    from infrastructure.persistence.repositories.label_repository import LabelRepository
    from infrastructure.persistence.repositories.message_repository import (
        AgeBuckets,
        LabelSizeStats,
        MessageRepository,
        SenderStats,
    )
    from infrastructure.persistence.repositories.snapshot_repository import SnapshotRepository
    from infrastructure.persistence.repositories.sync_state_repository import SyncStateRepository


@dataclass(frozen=True)
class StorageBreakdown:
    """Assembled data for the three storage breakdown charts on the dashboard.

    Attributes:
        top_senders_by_count: Top senders ranked by message count.
        top_senders_by_size:  Top senders ranked by total bytes.
        size_by_label:        User labels ranked by total attached message size.
        age_buckets:          Message counts split by calendar-year age band.
    """

    top_senders_by_count: list[SenderStats]
    top_senders_by_size: list[SenderStats]
    size_by_label: list[LabelSizeStats]
    age_buckets: AgeBuckets


class AnalyticsService:
    """
    Provides pre-computed analytics for the dashboard and reporting views.

    All methods are read-only.  Callers can invoke them freely without
    worrying about side effects or transaction ownership.

    Args:
        msg_repo:   Message repository for inbox/archive/sent/size queries.
        sync_repo:  Sync state repository for last-sync timestamp and threads.
        snap_repo:  Snapshot repository for historical progress data.
        label_repo: Label repository (reserved for future label analytics).
        settings:   Application settings (thresholds, history window, etc.).
    """

    def __init__(
        self,
        msg_repo: MessageRepository,
        sync_repo: SyncStateRepository,
        snap_repo: SnapshotRepository,
        label_repo: LabelRepository,
        settings: Settings,
    ) -> None:
        self._msg_repo = msg_repo
        self._sync_repo = sync_repo
        self._snap_repo = snap_repo
        self._label_repo = label_repo
        self._settings = settings

    # ── Dashboard summary ─────────────────────────────────────────────────────

    def dashboard_summary(self) -> DashboardSummary:
        """
        Compute all metrics needed to render the four goal-status cards.

        Metrics are fetched independently from each repository and assembled
        into a single frozen DashboardSummary DTO.

        Returns:
            DashboardSummary with all current counts and derived properties.
        """
        last_state = self._sync_repo.latest()
        ready_to_archive_label_id = self._label_id_for_name("ZeroApp/To-Archive")
        ready_to_delete_label_id = self._label_id_for_name("ZeroApp/To-Remove")

        return DashboardSummary(
            inbox_count=self._msg_repo.count_inbox(),
            inbox_size_bytes=self._msg_repo.inbox_size_bytes(),
            archive_unlabelled_count=self._msg_repo.count_archive_unlabelled(),
            sent_unresolved_count=self._msg_repo.count_sent_unresolved(),
            total_size_bytes=self._msg_repo.total_size_bytes(),
            custom_label_coverage_pct=self._msg_repo.custom_label_coverage_pct(),
            ready_to_archive_count=(
                self._msg_repo.count_search(
                    MessageFilter(
                        label_id=ready_to_archive_label_id,
                        is_inbox=True,
                        limit=1,
                    )
                )
                if ready_to_archive_label_id is not None
                else 0
            ),
            ready_to_delete_count=(
                self._msg_repo.count_search(
                    MessageFilter(
                        label_id=ready_to_delete_label_id,
                        is_inbox=True,
                        limit=1,
                    )
                )
                if ready_to_delete_label_id is not None
                else 0
            ),
            last_synced_at=last_state.last_synced_at if last_state is not None else None,
            old_inbox_thread_count=self._sync_repo.count_old_inbox_threads(
                threshold_days=self._settings.old_thread_threshold_days
            ),
        )

    # ── Sender analytics ──────────────────────────────────────────────────────

    def top_senders_by_count(self, limit: int = 10) -> list[SenderStats]:
        """
        Return the top senders ranked by message count.

        Args:
            limit: Number of senders to return (default 10).

        Returns:
            List of SenderStats, highest count first.
        """
        return self._msg_repo.top_senders_by_count(limit=limit)

    def top_senders_by_size(self, limit: int = 10) -> list[SenderStats]:
        """
        Return the top senders ranked by total message size.

        Args:
            limit: Number of senders to return (default 10).

        Returns:
            List of SenderStats, largest total bytes first.
        """
        return self._msg_repo.top_senders_by_size(limit=limit)

    # ── Historical snapshots ──────────────────────────────────────────────────

    def progress_snapshots(self, days: int = 30) -> list[DailySnapshot]:
        """
        Return the last ``days`` daily snapshots for the progress graphs.

        Args:
            days: Number of calendar days of history to return (default 30).

        Returns:
            List of DailySnapshot entities, oldest first (Chart.js time-series
            order).
        """
        return self._snap_repo.list_recent(days=days)

    def storage_breakdown(self, *, sender_limit: int = 5) -> StorageBreakdown:
        """Assemble all data needed for the three storage breakdown charts.

        Combines top-sender analytics, label size distribution, and age-bucket
        counts into a single frozen DTO for the dashboard.

        Args:
            sender_limit: Number of senders to include in each sender list.

        Returns:
            StorageBreakdown DTO with all chart data pre-computed.
        """
        return StorageBreakdown(
            top_senders_by_count=self._msg_repo.top_senders_by_count(
                limit=sender_limit
            ),
            top_senders_by_size=self._msg_repo.top_senders_by_size(
                limit=sender_limit
            ),
            size_by_label=self._msg_repo.size_by_label(limit=10),
            age_buckets=self._msg_repo.messages_by_age_bucket(),
        )

    # ── Thread analytics ──────────────────────────────────────────────────────

    def old_inbox_threads(self) -> list[Thread]:
        """
        Return inbox threads whose last message exceeds the configured age
        threshold (``settings.old_thread_threshold_days``).

        These are the primary targets of the Inbox Zero workflow — old, unresolved
        threads that have been sitting in the inbox without action.

        Returns:
            List of Thread entities sorted oldest-first.
        """
        return self._sync_repo.list_old_inbox_threads(
            threshold_days=self._settings.old_thread_threshold_days
        )

    def _label_id_for_name(self, name: str) -> str | None:
        """Return the local label ID for an exact display name, if present."""
        label = self._label_repo.get_by_name(name)
        return label.id if label is not None else None
