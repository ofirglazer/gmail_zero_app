"""
SnapshotRepository — persistence for daily progress snapshots.

Daily snapshots power the four progress graphs on the dashboard.
One row per calendar date — upserted at the end of each sync run so
the most recent sync's numbers are always reflected.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from infrastructure.persistence.models import DailySnapshotORM

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from domain.models.daily_snapshot import DailySnapshot


class SnapshotRepository:
    """
    Repository for daily snapshot persistence.

    Args:
        session: An open SQLAlchemy session.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(self, snapshot: DailySnapshot) -> None:
        """
        Insert or replace today's snapshot.

        On conflict (same ``snapshot_date``), all metric columns are updated
        with the new values so the most recent sync always wins.

        Args:
            snapshot: The DailySnapshot domain entity to persist.
        """
        stmt = sqlite_insert(DailySnapshotORM).values(
            snapshot_date=snapshot.snapshot_date,
            inbox_count=snapshot.inbox_count,
            inbox_size_bytes=snapshot.inbox_size_bytes,
            archive_unlabelled_count=snapshot.archive_unlabelled_count,
            sent_unresolved_count=snapshot.sent_unresolved_count,
            total_size_bytes=snapshot.total_size_bytes,
            custom_label_coverage_pct=snapshot.custom_label_coverage_pct,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["snapshot_date"],
            set_={
                "inbox_count": stmt.excluded.inbox_count,
                "inbox_size_bytes": stmt.excluded.inbox_size_bytes,
                "archive_unlabelled_count": stmt.excluded.archive_unlabelled_count,
                "sent_unresolved_count": stmt.excluded.sent_unresolved_count,
                "total_size_bytes": stmt.excluded.total_size_bytes,
                "custom_label_coverage_pct": stmt.excluded.custom_label_coverage_pct,
            },
        )
        self._session.execute(stmt)

    def get_by_date(self, snapshot_date: date) -> DailySnapshot | None:
        """
        Fetch the snapshot for a specific calendar date.

        Args:
            snapshot_date: The date to look up.

        Returns:
            DailySnapshot, or None if no snapshot exists for that date.
        """
        row = self._session.get(DailySnapshotORM, snapshot_date)
        return row.to_domain() if row else None

    def list_recent(self, *, days: int = 30) -> list[DailySnapshot]:
        """
        Return snapshots for the most recent ``days`` calendar days.

        Returns rows sorted oldest-first so Chart.js can consume them
        directly as a time series without re-sorting.

        Args:
            days: Number of days of history to return (default 30).

        Returns:
            List of DailySnapshot entities, oldest first.
        """
        cutoff = date.today() - timedelta(days=days)
        stmt = (
            select(DailySnapshotORM)
            .where(DailySnapshotORM.snapshot_date >= cutoff)
            .order_by(DailySnapshotORM.snapshot_date.asc())
        )
        rows = self._session.execute(stmt).scalars().all()
        return [r.to_domain() for r in rows]

    def latest(self) -> DailySnapshot | None:
        """
        Return the most recent snapshot, or None if none exist.

        Used by the dashboard to show the current state totals when no
        sync has run today yet.
        """
        stmt = (
            select(DailySnapshotORM)
            .order_by(DailySnapshotORM.snapshot_date.desc())
            .limit(1)
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return row.to_domain() if row else None

    def count(self) -> int:
        """Return the total number of snapshots stored."""
        from sqlalchemy import func

        stmt = select(func.count()).select_from(DailySnapshotORM)
        return self._session.execute(stmt).scalar_one()
