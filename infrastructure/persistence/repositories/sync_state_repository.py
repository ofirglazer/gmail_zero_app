"""
SyncStateRepository — persistence for sync state watermarks and threads.

Thread persistence is co-located here rather than in a separate ThreadRepository
because threads are always updated together with sync state in the sync pipeline
— they are part of the same unit of work.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from domain.models.sync_state import SyncState, SyncType
from infrastructure.persistence.models import SyncStateORM, ThreadORM

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from domain.models.thread import Thread


class SyncStateRepository:
    """
    Repository for sync state watermarks and thread aggregates.

    Args:
        session: An open SQLAlchemy session.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ── Sync state ────────────────────────────────────────────────────────────

    def save(self, state: SyncState) -> SyncState:
        """
        Persist a new sync state record.

        Always inserts a new row (never updates existing ones) — sync state
        history is append-only for diagnostic purposes.

        Args:
            state: The SyncState domain entity to persist.

        Returns:
            The persisted SyncState with its database-assigned ``id`` populated.
        """
        orm = SyncStateORM.from_domain(state)
        self._session.add(orm)
        self._session.flush()  # Populates orm.id without committing
        return orm.to_domain()

    def latest(self) -> SyncState | None:
        """
        Return the most recent sync state record.

        Used by SyncService to determine the starting historyId for the
        next incremental sync.

        Returns:
            The most recent SyncState, or None if no sync has ever run.
        """
        stmt = (
            select(SyncStateORM)
            .order_by(SyncStateORM.id.desc())
            .limit(1)
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return row.to_domain() if row else None

    def list_recent(self, *, limit: int = 20) -> list[SyncState]:
        """
        Return the most recent sync state records, newest first.

        Used by the Settings view to display sync history.

        Args:
            limit: Maximum number of records to return.

        Returns:
            List of SyncState entities, most recent first.
        """
        stmt = (
            select(SyncStateORM)
            .order_by(SyncStateORM.id.desc())
            .limit(limit)
        )
        rows = self._session.execute(stmt).scalars().all()
        return [r.to_domain() for r in rows]

    def has_ever_synced(self) -> bool:
        """
        Return True if at least one sync has completed successfully.

        Used by SyncService to decide between full and incremental sync
        on startup.
        """
        stmt = select(SyncStateORM.id).limit(1)
        return self._session.execute(stmt).scalar() is not None

    def last_full_sync(self) -> SyncState | None:
        """
        Return the most recent full sync record, if any.

        Args: None
        Returns:
            The most recent full SyncState, or None.
        """
        stmt = (
            select(SyncStateORM)
            .where(SyncStateORM.sync_type == SyncType.FULL.value)
            .order_by(SyncStateORM.id.desc())
            .limit(1)
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return row.to_domain() if row else None

    # ── Thread persistence ────────────────────────────────────────────────────

    def upsert_thread(self, thread: Thread) -> None:
        """
        Insert or update a thread record.

        Called by the sync pipeline whenever a message in a thread changes.
        Upserts are idempotent — safe to call multiple times for the same thread.

        Args:
            thread: The Thread domain entity to persist.
        """
        stmt = sqlite_insert(ThreadORM).values(
            id=thread.id,
            subject=thread.subject,
            message_count=thread.message_count,
            snippet=thread.snippet,
            last_message_at=thread.last_message_at,
            is_inbox=thread.is_inbox,
            has_custom_label=thread.has_custom_label,
            last_synced_at=thread.last_synced_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "subject": stmt.excluded.subject,
                "message_count": stmt.excluded.message_count,
                "snippet": stmt.excluded.snippet,
                "last_message_at": stmt.excluded.last_message_at,
                "is_inbox": stmt.excluded.is_inbox,
                "has_custom_label": stmt.excluded.has_custom_label,
                "last_synced_at": stmt.excluded.last_synced_at,
            },
        )
        self._session.execute(stmt)

    def upsert_threads(self, threads: list[Thread]) -> None:
        """Upsert a collection of threads."""
        for thread in threads:
            self.upsert_thread(thread)

    def get_thread_by_id(self, thread_id: str) -> Thread | None:
        """
        Fetch a single thread by its Gmail thread ID.

        Args:
            thread_id: Gmail thread ID.

        Returns:
            Domain Thread entity, or None if not found.
        """
        row = self._session.get(ThreadORM, thread_id)
        return row.to_domain() if row else None

    def list_old_inbox_threads(
        self, *, threshold_days: int = 30, limit: int = 100
    ) -> list[Thread]:
        """
        Return inbox threads whose last message is older than ``threshold_days``.

        Used by the dashboard "Old Unresolved Threads" panel.

        Args:
            threshold_days: Threads older than this many days are returned.
            limit:          Maximum number of threads to return.

        Returns:
            List of Thread entities, oldest first.
        """
        from datetime import UTC, datetime, timedelta

        cutoff = datetime.now(tz=UTC) - timedelta(days=threshold_days)
        stmt = (
            select(ThreadORM)
            .where(
                ThreadORM.is_inbox.is_(True),
                ThreadORM.last_message_at <= cutoff,
            )
            .order_by(ThreadORM.last_message_at.asc())
            .limit(limit)
        )
        rows = self._session.execute(stmt).scalars().all()
        return [r.to_domain() for r in rows]

    def count_old_inbox_threads(self, *, threshold_days: int = 30) -> int:
        """
        Return count of inbox threads older than ``threshold_days``.

        Args:
            threshold_days: Age threshold in days.

        Returns:
            Integer count.
        """
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import func

        cutoff = datetime.now(tz=UTC) - timedelta(days=threshold_days)
        stmt = select(func.count()).where(
            ThreadORM.is_inbox.is_(True),
            ThreadORM.last_message_at <= cutoff,
        )
        return self._session.execute(stmt).scalar_one()
